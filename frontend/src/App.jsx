/** Purpose: Implements the incident workflow user interface. Used by main.jsx as the root React component. */

import { useMemo, useState } from 'react';
import { api, apiErrorMessage } from './api';

const initialForm = { incident_text: '' };

function renderValue(value) {
  if (value === null || value === undefined || value === '') return 'â€”';
  if (Array.isArray(value)) return value.length ? value.map(renderValue).join(', ') : 'â€”';
  if (typeof value === 'object') return JSON.stringify(value, null, 2);
  return String(value);
}

function fieldLabel(key) {
  return key.replace(/_/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function FieldValue({ value }) {
  if (value === null || value === undefined || value === '') return <span className="empty-value">Not provided</span>;
  if (Array.isArray(value)) {
    if (!value.length) return <span className="empty-value">None</span>;
    return <ul className="value-list">{value.map((item, index) =>
      <li key={index}>{typeof item === 'object' ? <NestedFields value={item} /> : String(item)}</li>
    )}</ul>;
  }
  if (typeof value === 'object') return <NestedFields value={value} />;
  return <span>{String(value)}</span>;
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
    setError('');
  };

  const editableFields = Object.entries(pendingResult || {}).filter(
    ([key]) => key !== 'diagnostics',
  );
  const fields = Object.entries(pendingResult || {}).flatMap(([key, value]) => {
    if (key !== 'diagnostics') return [[key, value]];
    return [['hipo_review', value?.hipo_review_required ? 'Yes' : 'No']];
  });

  return (
    <div className="app-shell">
      <main className="container">
        <header className="header-card">
          <div><h1>Incident Assistant</h1></div>
          <p className="subtle">Generate the complete analysis, review it once, and save.</p>
        </header>

        <section className="panel">
          <label className="textarea-label" htmlFor="incident-text">Incident narrative</label>
          <textarea id="incident-text" value={form.incident_text}
            onChange={(event) => setForm({ incident_text: event.target.value })}
            placeholder="Paste the incident report or narrative here..." disabled={loading} />
          <button className="primary-btn" onClick={startAnalysis}
            disabled={loading || !form.incident_text.trim()}>
            {loading && !sessionId ? 'Analyzing...' : 'Start analysis'}
          </button>
        </section>

        {error && <div className="error-box" role="alert">{error}</div>}

        {stepInfo && <section className="panel">
          <div className="progress-row"><strong>{progressLabel}</strong></div>

          {pendingResult && <>
            <div className="result-card">
              <h3>{stepInfo.step_title || 'Current step result'}</h3>
              <div className="analysis-groups">
                {fields.map(([key, value]) => <section
                  key={key}
                  className={`analysis-group${key === 'domain' || key === 'subdomain' ? ' inline-value' : ''}`}
                >
                  <h4>{fieldLabel(key)}</h4>
                  <FieldValue value={value} />
                </section>)}
              </div>
            </div>

            <div className="confirmation-card">
              <h3>{stepInfo.question || 'Is this response correct?'}</h3>
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
          </>}

          {completed && <div className="final-card">
            <h3>Response saved</h3><p>The confirmed incident record has been saved to MongoDB.</p>
            <button className="primary-btn" onClick={reset}>Start new incident</button>
          </div>}
        </section>}
      </main>
    </div>
  );
}

export default App;
