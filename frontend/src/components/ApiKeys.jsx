import React, { useEffect, useState } from 'react';
import api from '../api';

const ApiKeys = ({ onChanged }) => {
  const [settings, setSettings] = useState(null);
  const [provider, setProvider] = useState('GEMINI');
  const [geminiKey, setGeminiKey] = useState('');
  const [accountId, setAccountId] = useState('');
  const [cloudflareToken, setCloudflareToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const load = async () => {
    const { data } = await api.get('/account/api-keys');
    setSettings(data);
    setProvider(data.selected === 'CLOUDFLARE' ? 'CLOUDFLARE' : 'GEMINI');
    setAccountId(data.cloudflare?.account_id || '');
  };

  useEffect(() => { load().catch(() => setError('Could not load provider settings.')); }, []);

  const run = async (request) => {
    setBusy(true);
    setError('');
    try {
      await request();
      setGeminiKey('');
      setCloudflareToken('');
      await load();
      onChanged?.();
    } catch (err) {
      setError(err.response?.data?.detail || 'Could not update provider settings. Try again.');
    } finally {
      setBusy(false);
    }
  };

  const save = () => provider === 'GEMINI'
    ? run(() => api.put('/account/api-keys', { gemini_api_key: geminiKey.trim() }))
    : run(() => api.put('/account/api-keys/cloudflare', {
        account_id: accountId.trim(), api_token: cloudflareToken.trim(),
      }));

  const useSaved = () => run(() => api.put('/account/provider', { provider }));
  const remove = () => run(() => api.delete(`/account/api-keys/${provider.toLowerCase()}`));
  const saved = settings?.[provider.toLowerCase()];
  const active = settings?.selected === provider;

  if (!settings) return <div className="settings-section">{error || 'Loading provider settings…'}</div>;

  return (
    <div className="settings-section">
      <h4 className="settings-heading">AI provider</h4>
      <p className="modal-message">
        {settings?.selected === 'FREE'
          ? 'Using daily free credits with the app’s key.'
          : `Using your ${settings?.selected === 'GEMINI' ? 'Gemini' : 'Cloudflare'} credentials — no daily message credits.`}
      </p>
      <label htmlFor="provider-select">Use your own provider</label>
      <select id="provider-select" className="settings-input" value={provider}
        onChange={(e) => setProvider(e.target.value)} disabled={busy}>
        <option value="GEMINI">Gemini</option>
        <option value="CLOUDFLARE">Cloudflare Workers AI</option>
      </select>
      {saved?.saved && (
        <div className="settings-saved">
          <span>Saved credentials ending in ••••{saved.last4}</span>
          <button className="modal-btn" onClick={remove} disabled={busy}>Remove</button>
        </div>
      )}
      {provider === 'GEMINI' ? (
        <>
          <a href="https://aistudio.google.com/app/apikey" target="_blank" rel="noreferrer"
            style={{ display: 'inline-block', marginTop: '0.75rem' }}>Get a Gemini key</a>
          <input className="settings-input" type="password" autoComplete="off"
            aria-label="Gemini API key" placeholder="Gemini API key"
            value={geminiKey} onChange={(e) => setGeminiKey(e.target.value)} disabled={busy} />
        </>
      ) : (
        <>
          <p className="modal-message">Get a Workers AI API token and Account ID in your Cloudflare dashboard.</p>
          <input className="settings-input" type="text" autoComplete="off"
            aria-label="Cloudflare Account ID" placeholder="Cloudflare Account ID"
            value={accountId} onChange={(e) => setAccountId(e.target.value)} disabled={busy} />
          <input className="settings-input" type="password" autoComplete="off"
            aria-label="Cloudflare API token" placeholder="Cloudflare Workers AI API token"
            value={cloudflareToken} onChange={(e) => setCloudflareToken(e.target.value)} disabled={busy} />
        </>
      )}
      {error && <p className="settings-error" role="alert">{error}</p>}
      <div className="modal-actions">
        {saved?.saved && !active && (
          <button className="modal-btn" onClick={useSaved} disabled={busy}>Use saved credentials</button>
        )}
        <button className="modal-btn modal-btn-primary" onClick={save}
          disabled={busy || (provider === 'GEMINI' ? !geminiKey.trim()
            : !accountId.trim() || !cloudflareToken.trim())}>
          {busy ? 'Checking…' : 'Save and use'}
        </button>
      </div>
    </div>
  );
};

export default ApiKeys;
