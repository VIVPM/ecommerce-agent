// Lets a shopper save their own Gemini key, which replaces the app's and lifts the daily credit limit.
import React, { useEffect, useState } from 'react';
import api from '../api';

const ApiKeys = ({ onChanged }) => {
  const [saved, setSaved] = useState(null);
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const load = () => api.get('/account/api-keys').then(res => setSaved(res.data.gemini)).catch(() => {});

  useEffect(() => { load(); }, []);

  const run = async (request) => {
    setBusy(true);
    setError('');
    try {
      await request();
      setValue('');
      await load();
      onChanged?.();
    } catch (err) {
      setError(err.response?.data?.detail || 'Could not update the API key. Try again.');
    } finally {
      setBusy(false);
    }
  };

  const save = () => run(() => api.put('/account/api-keys', { gemini_api_key: value.trim() }));
  const remove = () => run(() => api.delete('/account/api-keys/gemini'));

  return (
    <div className="settings-section">
      <h4 className="settings-heading">Gemini API key</h4>
      <p className="modal-sub">
        {saved?.saved
          ? 'Using your own key — no daily message limit.'
          : "Using the app's key — daily credits apply. Add your own to remove the limit."}{' '}
        <a href="https://aistudio.google.com/app/apikey" target="_blank" rel="noreferrer">Get a key</a>
      </p>
      {saved?.saved && (
        <div className="settings-saved">
          <span>Saved key ending in ••••{saved.last4}</span>
          <button className="modal-btn-ghost" onClick={remove} disabled={busy}>Remove</button>
        </div>
      )}
      <input
        className="settings-input"
        type="password"
        autoComplete="off"
        value={value}
        placeholder={saved?.saved ? 'Enter a new key to replace it' : 'AIza…'}
        onChange={(e) => setValue(e.target.value)}
        disabled={busy}
      />
      {error && <p className="settings-error">{error}</p>}
      <div className="modal-actions">
        <button className="modal-btn-primary" onClick={save} disabled={busy || !value.trim()}>
          {busy ? 'Checking…' : 'Save key'}
        </button>
      </div>
    </div>
  );
};

export default ApiKeys;
