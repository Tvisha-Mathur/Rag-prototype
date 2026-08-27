/** Purpose: Implements the incident workflow user interface. Used by main.jsx as the root React component. */

import { useEffect, useMemo, useRef, useState } from 'react';
import { api, apiErrorMessage } from './api';

const initialForm = { incident_text: '' };
const intakeQuestions = [
  {
    key: 'initiating_event',
    prompt: 'What happened? Describe the initiating event in one or two sentences.',
    label: 'Initiating event',
  },
  {
    key: 'exposure',
    prompt: 'Who or what was exposed or affected?',
    label: 'Affected or exposed party',
  },
  {
    key: 'hazard',
    prompt: 'What hazard or event mechanism was involved?',
    label: 'Hazard or mechanism',
  },
  {
    key: 'actual_consequence',
    prompt: 'What was the actual consequence, including no injury or no damage if applicable?',
    label: 'Actual consequence',
  },
  {
    key: 'escalation_proximity',
    prompt: 'How close was the event to a more severe outcome, and what small change could have made it worse?',
    label: 'Escalation proximity and credible worse outcome',
  },
  {
    key: 'controls',
    prompt: 'Which control failed, was missing, or was restored? Include the immediate action taken.',
    label: 'Controls and immediate response',
  },
  {
    key: 'assets_operations',
    prompt: 'Were assets or operations affected? Include damage, downtime, disruption, or available workarounds.',
    label: 'Asset and operational impact',
  },
  {
    key: 'reputation_vip',
    prompt: 'Was there reputational exposure, a complaint, publicity, regulatory involvement, or VIP involvement?',
    label: 'Reputation and VIP involvement',
  },
];
const hiddenFrontendFields = new Set([
  'location',
  'hipo_classification',
  'affected_party_details',
]);

const mojibakeReplacements = new Map([
  ['\u00e2\u20ac\u201d', '\u2014'],
  ['\u00e2\u20ac\u201c', '\u2013'],
  ['\u00e2\u20ac\u02dc', '\u2018'],
  ['\u00e2\u20ac\u2122', '\u2019'],
  ['\u00e2\u20ac\u0153', '\u201c'],
  ['\u00e2\u20ac\u009d', '\u201d'],
  ['\u00e2\u20ac\u00a6', '\u2026'],
  ['\u00c2\u00a0', ' '],
]);

function normalizeDisplayText(value) {
  let text = String(value);
  mojibakeReplacements.forEach((replacement, corrupted) => {
    text = text.split(corrupted).join(replacement);
  });
  return text;
}

function renderValue(value) {
  if (value === null || value === undefined || value === '') return '\u2014';
  if (Array.isArray(value)) return value.length ? value.map(renderValue).join(', ') : '\u2014';
  if (typeof value === 'object') return normalizeDisplayText(JSON.stringify(value, null, 2));
  return normalizeDisplayText(value);
}

function fieldLabel(key) {
  return normalizeDisplayText(key).replace(/_/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function FieldValue({ value }) {
  if (value === null || value === undefined || value === '') return <span className="empty-value">Not provided</span>;
  if (Array.isArray(value)) {
    if (!value.length) return <span className="empty-value">None</span>;
    return <ul className="value-list">{value.map((item, index) =>
      <li key={index}>{typeof item === 'object' ? <NestedFields value={item} /> : normalizeDisplayText(item)}</li>
    )}</ul>;
  }
  if (typeof value === 'object') return <NestedFields value={value} />;
  return <span>{normalizeDisplayText(value)}</span>;
}

function NestedFields({ value }) {
  return <div className="nested-fields">{Object.entries(value).map(([key, item]) =>
    <div className="detail-field" key={key}>
      <span className="field-label">{fieldLabel(key)}</span>
      <FieldValue value={item} />
    </div>
  )}</div>;
}

function App() {
  const [form, setForm] = useState(initialForm);
  const [messageDraft, setMessageDraft] = useState('');
  const [intakeAnswers, setIntakeAnswers] = useState([]);
  const [sessionId, setSessionId] = useState('');
  const [stepInfo, setStepInfo] = useState(null);
  const [pendingResult, setPendingResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [completed, setCompleted] = useState(false);
  const [editingCorrection, setEditingCorrection] = useState(false);
  const [correctionDraft, setCorrectionDraft] = useState({});
  const [submittedNarrative, setSubmittedNarrative] = useState('');
  const chatWindowRef = useRef(null);

  const progressLabel = useMemo(() => {
    if (!stepInfo) return 'Start a new incident review';
    if (stepInfo.completed) return 'Completed';
    return 'Complete incident analysis';
  }, [stepInfo]);

  const applyResponse = (data) => {
    setStepInfo(data);
    setPendingResult(data.result || null);
    setCompleted(Boolean(data.completed));
    setEditingCorrection(false);
    setCorrectionDraft({});
  };

  const assembledNarrative = useMemo(() => intakeAnswers.map((answer, index) => (
    `${intakeQuestions[index].label}: ${answer}`
  )).join(' '), [intakeAnswers]);

  const intakeComplete = intakeAnswers.length === intakeQuestions.length;

  useEffect(() => {
    const windowElement = chatWindowRef.current;
    if (windowElement) windowElement.scrollTo({ top: windowElement.scrollHeight, behavior: 'smooth' });
  }, [intakeAnswers, loading, pendingResult, completed, error]);

  const submitIntakeAnswer = () => {
    const answer = messageDraft.trim();
    if (!answer || intakeComplete || loading || stepInfo) return;
    const updatedAnswers = [...intakeAnswers, answer];
    setIntakeAnswers(updatedAnswers);
    setMessageDraft('');
    if (updatedAnswers.length === intakeQuestions.length) {
      setForm({
        incident_text: updatedAnswers.map((item, index) => (
          `${intakeQuestions[index].label}: ${item}`
        )).join(' '),
      });
    }
  };

  const startAnalysis = async () => {
    const narrative = form.incident_text.trim();
    if (!narrative) return;
    setSubmittedNarrative(narrative);
    setLoading(true);
    setError('');
    setCompleted(false);
    try {
      const { data: started } = await api.post('/incident/workflow/start-async', form);
      setSessionId(started.session_id);

      let finished = null;
      let consecutivePollFailures = 0;
      for (let attempt = 0; attempt < 150; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 2000));
        let job;
        try {
          const response = await api.get(
            `/incident/workflow/${started.session_id}/status`,
          );
          job = response.data;
          consecutivePollFailures = 0;
        } catch (pollError) {
          // Render free services can briefly return 502/503 while waking or
          // restarting. Keep polling the persisted MongoDB job instead of
          // discarding an otherwise valid analysis after one transient error.
          consecutivePollFailures += 1;
          if (consecutivePollFailures < 10) continue;
          throw pollError;
        }
        if (job.status === 'failed') {
          throw new Error(job.error || 'Incident analysis failed.');
        }
        if (job.status === 'completed') {
          finished = job.result;
          break;
        }
      }

      if (!finished) {
        throw new Error('Incident analysis is still processing after five minutes.');
      }
      applyResponse(finished);
    } catch (err) {
      setError(err.response ? apiErrorMessage(err, 'Unable to start the workflow.') : err.message);
    } finally {
      setLoading(false);
    }
  };

  const confirmStep = async (approved) => {
    setLoading(true);
    setError('');
    try {
      const correction = Object.fromEntries(Object.entries(correctionDraft).map(([key, value]) => {
        if (typeof pendingResult?.[key] !== 'object') return [key, value];
        try { return [key, JSON.parse(value)]; } catch { return [key, value]; }
      }));
      const payload = approved ? { approved: true } : { approved: false, correction };
      const { data } = await api.post(`/incident/workflow/${sessionId}/confirm`, payload);
      applyResponse(data);
    } catch (err) {
      setError(apiErrorMessage(err, 'Unable to confirm the current step.'));
    } finally {
      setLoading(false);
    }
  };

  const reset = () => {
    setForm(initialForm);
    setMessageDraft('');
    setIntakeAnswers([]);
    setSessionId('');
    setStepInfo(null);
    setPendingResult(null);
    setCompleted(false);
    setEditingCorrection(false);
    setCorrectionDraft({});
    setSubmittedNarrative('');
    setError('');
  };

  const editableFields = Object.entries(pendingResult || {}).filter(
    ([key]) => key !== 'diagnostics' && !hiddenFrontendFields.has(key),
  );
  const fields = Object.entries(pendingResult || {}).flatMap(([key, value]) => {
    if (hiddenFrontendFields.has(key)) return [];
    if (key !== 'diagnostics') return [[key, value]];
    return [['final_hipo_review', value?.hipo_review_required ? 'Yes' : 'No']];
  });

  return (
    <div className="app-shell">
      <main className="chat-container">
        <header className="chat-header">
          <div className="assistant-avatar" aria-hidden="true">IA</div>
          <div>
            <h1>Incident Assistant</h1>
            <p className="subtle">RAG and CRAG incident analysis</p>
          </div>
          {(stepInfo || submittedNarrative || intakeAnswers.length > 0) && <button className="new-chat-btn" onClick={reset} disabled={loading}>
            New analysis
          </button>}
        </header>

        <section className="chat-window" aria-live="polite" ref={chatWindowRef}>
          {!intakeAnswers.length && !stepInfo && <div className="message-row assistant-row">
            <div className="message-avatar">IA</div>
            <div className="message-bubble assistant-bubble">
              <p>I will collect the information required for HIPO assessment one step at a time. Enter <strong>Unknown</strong> when a fact is unavailable.</p>
            </div>
          </div>}

          {submittedNarrative && !intakeAnswers.length && <div className="message-row user-row">
            <div className="message-bubble user-bubble">{submittedNarrative}</div>
          </div>}

          {intakeQuestions.map((question, index) => {
            if (index > intakeAnswers.length) return null;
            if (index === intakeAnswers.length && intakeComplete) return null;
            return <div className="intake-exchange" key={question.key}>
              <div className="message-row assistant-row">
                <div className="message-avatar">IA</div>
                <div className="message-bubble assistant-bubble">
                  <span className="question-progress">Step {index + 1} of {intakeQuestions.length}</span>
                  <p>{question.prompt}</p>
                </div>
              </div>
              {intakeAnswers[index] && <div className="message-row user-row">
                <div className="message-bubble user-bubble">{intakeAnswers[index]}</div>
              </div>}
            </div>;
          })}

          {intakeComplete && !submittedNarrative && <div className="message-row assistant-row">
            <div className="message-avatar">IA</div>
            <div className="message-bubble assistant-bubble intake-review">
              <span className="question-progress">Ready for analysis</span>
              <h3>Review the assembled incident narrative</h3>
              <p>{assembledNarrative}</p>
              <div className="button-row">
                <button className="primary-btn" onClick={startAnalysis} disabled={loading}>Run analysis</button>
                <button className="secondary-btn" onClick={reset} disabled={loading}>Start again</button>
              </div>
            </div>
          </div>}

          {loading && !pendingResult && <div className="message-row assistant-row">
            <div className="message-avatar">IA</div>
            <div className="message-bubble assistant-bubble typing-bubble">
              <span /><span /><span />
              <strong>Analyzing the incident...</strong>
            </div>
          </div>}

          {error && <div className="message-row assistant-row">
            <div className="message-avatar error-avatar">!</div>
            <div className="message-bubble error-bubble" role="alert">{error}</div>
          </div>}

          {stepInfo && pendingResult && <div className="message-row assistant-row">
            <div className="message-avatar">IA</div>
            <div className="message-bubble assistant-bubble analysis-message">
              <div className="message-heading">
                <div>
                  <span className="status-label">{progressLabel}</span>
                  <h3>{normalizeDisplayText(stepInfo.step_title || 'Incident analysis')}</h3>
                </div>
              </div>

              <div className="analysis-groups">
                {fields.map(([key, value]) => <section
                  key={key}
                  className={`analysis-group${key === 'domain' || key === 'subdomain' ? ' inline-value' : ''}`}
                >
                  <h4>{fieldLabel(key)}</h4>
                  <FieldValue value={value} />
                </section>)}
              </div>

            <div className="chat-confirmation">
              <h3>{normalizeDisplayText(stepInfo.question || 'Is this response correct?')}</h3>
              {!editingCorrection ? <div className="button-row">
                <button className="confirm-yes" onClick={() => confirmStep(true)} disabled={loading}>
                  {loading ? 'Saving...' : 'Save response'}
                </button>
                <button className="confirm-no" onClick={() => setEditingCorrection(true)} disabled={loading}>
                  Edit response
                </button>
              </div> : <div className="correction-panel">
                <h3>Edit the current response</h3>
                {editableFields.map(([key, value]) => <label key={key} className="field-editor">
                  <span>{key.replace(/_/g, ' ')}</span>
                  <textarea value={correctionDraft[key] ?? renderValue(value)}
                    onChange={(event) => setCorrectionDraft((current) => ({ ...current, [key]: event.target.value }))} />
                </label>)}
                <div className="button-row">
                  <button className="primary-btn" onClick={() => confirmStep(false)} disabled={loading}>
                    {loading ? 'Saving...' : 'Save correction & continue'}
                  </button>
                  <button className="secondary-btn" onClick={() => { setEditingCorrection(false); setCorrectionDraft({}); }} disabled={loading}>Cancel</button>
                </div>
              </div>}
            </div>
            </div>
          </div>}

          {completed && <div className="message-row assistant-row">
            <div className="message-avatar">IA</div>
            <div className="message-bubble assistant-bubble saved-message">
              <h3>Response saved</h3>
              <p>The confirmed incident record has been saved to MongoDB.</p>
              <button className="primary-btn" onClick={reset}>Start new incident</button>
            </div>
          </div>}
        </section>

        <section className="chat-composer">
          <label className="sr-only" htmlFor="incident-text">Incident narrative</label>
          <textarea id="incident-text" value={messageDraft}
            onChange={(event) => setMessageDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                submitIntakeAnswer();
              }
            }}
            placeholder={intakeComplete ? 'Review the narrative above to continue' : 'Type your answer...'}
            disabled={loading || Boolean(stepInfo) || intakeComplete} />
          <button className="send-btn" onClick={submitIntakeAnswer}
            disabled={loading || Boolean(stepInfo) || intakeComplete || !messageDraft.trim()}>
            {loading ? 'Analyzing' : 'Send'}
          </button>
          <p className="composer-hint">Press Enter to send · Shift + Enter for a new line</p>
        </section>
      </main>
    </div>
  );
}

export default App;
