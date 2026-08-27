/** Purpose: Implements the incident workflow user interface. Used by main.jsx as the root React component. */

import { useMemo, useState } from 'react';
import { api, apiErrorMessage } from './api';

const initialForm = { incident_text: '' };
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
  const [sessionId, setSessionId] = useState('');
  const [stepInfo, setStepInfo] = useState(null);
  const [pendingResult, setPendingResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [completed, setCompleted] = useState(false);
  const [editingCorrection, setEditingCorrection] = useState(false);
  const [correctionDraft, setCorrectionDraft] = useState({});
  const [submittedNarrative, setSubmittedNarrative] = useState('');

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
          {(stepInfo || submittedNarrative) && <button className="new-chat-btn" onClick={reset} disabled={loading}>
            New analysis
          </button>}
        </header>

        <section className="chat-window" aria-live="polite">
          {!submittedNarrative && !stepInfo && <div className="message-row assistant-row">
            <div className="message-avatar">IA</div>
            <div className="message-bubble assistant-bubble">
              <p>Send me an incident narrative. I will return a concise factual summary, approved taxonomy, parameter scores, and the final HIPO review status.</p>
            </div>
          </div>}

          {submittedNarrative && <div className="message-row user-row">
            <div className="message-bubble user-bubble">{submittedNarrative}</div>
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
          <textarea id="incident-text" value={form.incident_text}
            onChange={(event) => setForm({ incident_text: event.target.value })}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                if (!loading && form.incident_text.trim()) startAnalysis();
              }
            }}
            placeholder="Describe the incident..." disabled={loading || Boolean(stepInfo)} />
          <button className="send-btn" onClick={startAnalysis}
            disabled={loading || Boolean(stepInfo) || !form.incident_text.trim()}>
            {loading ? 'Analyzing' : 'Send'}
          </button>
          <p className="composer-hint">Press Enter to send · Shift + Enter for a new line</p>
        </section>
      </main>
    </div>
  );
}

export default App;
