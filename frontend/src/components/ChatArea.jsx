import React, { useState, useEffect, useRef } from 'react';
import { Send, ShoppingBag, Heart, Zap, ShoppingCart, ImagePlus, X } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import api from '../api';

const PID_RE = /[?&]pid=([A-Za-z0-9]+)/;

const IMG_MARKER = '\n[[SHOEIMG]]';

const forceLogout = () => {
  localStorage.removeItem('token');
  localStorage.removeItem('user');
  localStorage.removeItem('login_time');
  window.location.reload();
};

const MAX_STREAM_RETRIES = 5;
const RETRY_DELAY_MS = 700;

const FOLLOW_UPS = {
  search_product_database: [
    'Any cheaper ones?',
    'Only 4 stars and above',
    'Which is the best value?',
  ],
  search_faq_knowledge_base: [
    'What is your return policy?',
    'Do you accept cash on delivery?',
    'How long does delivery take?',
  ],
  manage_saved: [
    'Compare my saved items',
    'Add saved items 1 and 2 to my cart',
    'Which has the most ratings?',
  ],
  manage_orders: [
    'Place my order',
    'What have I ordered?',
    'Show my saved items',
  ],
};

const FOLLOW_UPS_NO_RESULTS = [
  'Show Nike shoes under 3000',
  'Best rated shoes under 2000',
  'Cheapest running shoes for men',
];

const ChatArea = ({
  currentChatId,
  chats,
  messages,
  onChatUpdated,
  onNewChatCreated,
  savedPids,
  onToggleSave,
  cartPids,
  onToggleCart,
  credits,
  onCreditsRefresh,
}) => {
  const outOfCredits = credits && credits.remaining <= 0;
  const [input, setInput] = useState('');
  const [image, setImage] = useState(null);
  const [loading, setLoading] = useState(false);
  const [optimisticMsg, setOptimisticMsg] = useState(null);
  const [statusMsg, setStatusMsg] = useState('');
  const [streamingMsg, setStreamingMsg] = useState('');
  const [suggestions, setSuggestions] = useState([]);
  const scrollRef = useRef(null);

  const markdownComponents = {
    a: ({ node, href, children, ...props }) => {
      const match = PID_RE.exec(href || '');
      const link = (
        <a href={href} target="_blank" rel="noopener noreferrer" {...props}>
          {children}
        </a>
      );
      if (!match || !onToggleSave) return link;

      const pid = match[1].toUpperCase();
      const isSaved = savedPids?.has(pid);
      const inCart = cartPids?.has(pid);
      return (
        <>
          {link}
          <button
            type="button"
            className={`save-btn${isSaved ? ' saved' : ''}`}
            title={isSaved ? 'Remove from saved' : 'Save this product'}
            aria-label={isSaved ? 'Remove from saved' : 'Save this product'}
            aria-pressed={!!isSaved}
            onClick={() => onToggleSave(pid)}
          >
            <Heart size={13} fill={isSaved ? 'currentColor' : 'none'} />
          </button>
          {onToggleCart && (
            <button
              type="button"
              className={`save-btn${inCart ? ' saved' : ''}`}
              title={inCart ? 'Remove from cart' : 'Add to cart'}
              aria-label={inCart ? 'Remove from cart' : 'Add to cart'}
              aria-pressed={!!inCart}
              onClick={() => onToggleCart(pid)}
            >
              <ShoppingCart size={13} fill={inCart ? 'currentColor' : 'none'} />
            </button>
          )}
        </>
      );
    },
  };

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, loading, optimisticMsg, streamingMsg]);

  const fileRef = useRef(null);

  const handleImagePick = (e) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = String(reader.result);
      const b64 = dataUrl.split(',')[1] || '';
      const imgEl = new Image();
      imgEl.onload = () => {
        const max = 180;
        const scale = Math.min(1, max / Math.max(imgEl.width, imgEl.height));
        const w = Math.round(imgEl.width * scale);
        const h = Math.round(imgEl.height * scale);
        const canvas = document.createElement('canvas');
        canvas.width = w;
        canvas.height = h;
        canvas.getContext('2d').drawImage(imgEl, 0, 0, w, h);
        let thumb = '';
        try { thumb = canvas.toDataURL('image/jpeg', 0.6); } catch { thumb = ''; }
        setImage({ b64, mime: file.type || 'image/jpeg', name: file.name, thumb });
      };
      imgEl.onerror = () => setImage({ b64, mime: file.type || 'image/jpeg', name: file.name, thumb: '' });
      imgEl.src = dataUrl;
    };
    reader.readAsDataURL(file);
  };

  const renderUserContent = (content) => {
    const [txt, img] = (content || '').split(IMG_MARKER);
    if (!img) return txt;
    return (
      <div className="user-content">
        <img className="msg-img" src={img} alt="uploaded" />
        {txt && <span>{txt}</span>}
      </div>
    );
  };

  const handleSend = async (e, preset) => {
    e?.preventDefault();
    const userQuery = (preset ?? input).trim();
    const img = preset ? null : image;
    if ((!userQuery && !img) || loading || outOfCredits) return;

    setInput('');
    setImage(null);
    setLoading(true);
    setStatusMsg('');
    setStreamingMsg('');
    setSuggestions([]);
    setOptimisticMsg((userQuery || (img ? 'Image search' : '')) +
                     (img?.thumb ? `${IMG_MARKER}${img.thumb}` : ''));

    const history = messages.slice(-5).map((m) => ({
      ...m,
      content: (m.content || '').split(IMG_MARKER)[0],
    }));

    try {
      let chatId = currentChatId;

      if (!chatId) {
        const newChatRes = await api.post('/chats/new');
        chatId = newChatRes.data.chat_id;
        onNewChatCreated(chatId, newChatRes.data.chat);
      }

      const token = localStorage.getItem('token');
      const idempotencyKey = crypto.randomUUID();
      const res = await fetch(`${api.defaults.baseURL}/chats/${chatId}/message`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
          'Idempotency-Key': idempotencyKey,
        },
        body: JSON.stringify({
          query: userQuery,
          history,
          ...(img ? { image: img.b64, image_mime: img.mime, image_thumb: img.thumb } : {}),
        }),
      });

      if (res.status === 401) {
        forceLogout();
        return;
      }
      if (res.status === 429) {
        const info = await res.json().catch(() => ({}));
        onCreditsRefresh?.();
        const existingChat = chats[chatId] || {};
        onChatUpdated(chatId, {
          ...existingChat,
          messages: [
            ...(existingChat.messages || []),
            { role: 'assistant', content: info.detail || 'Daily message limit reached. Resets tomorrow.' },
          ],
        });
        return;
      }
      if (!res.ok) {
        throw new Error(`Request failed (${res.status})`);
      }

      const { job_id: jobId } = await res.json();

      let streamed = '';
      let doneChat = null;
      let streamError = null;
      let seq = 0;
      let finished = false;
      let retries = 0;

      while (!finished) {
        let evRes;
        try {
          evRes = await fetch(
            `${api.defaults.baseURL}/jobs/${jobId}/events?after=${seq}`,
            { headers: { Authorization: `Bearer ${token}` } }
          );
        } catch {
          if (++retries > MAX_STREAM_RETRIES) { streamError = 'Lost connection. Please try again.'; break; }
          setStatusMsg('Reconnecting...');
          await new Promise((r) => setTimeout(r, RETRY_DELAY_MS * retries));
          continue;
        }
        if (evRes.status === 401) { forceLogout(); return; }
        if (!evRes.ok || !evRes.body) {
          if (++retries > MAX_STREAM_RETRIES) { streamError = 'Lost connection. Please try again.'; break; }
          await new Promise((r) => setTimeout(r, RETRY_DELAY_MS * retries));
          continue;
        }

        const reader = evRes.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        try {
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });

            const events = buffer.split('\n\n');
            buffer = events.pop();

            for (const evt of events) {
              const line = evt.trim();
              if (!line.startsWith('data:')) continue;
              let payload;
              try {
                payload = JSON.parse(line.slice(5).trim());
              } catch {
                continue;
              }
              if (typeof payload.seq === 'number') seq = payload.seq;
              if (payload.type === 'status') {
                setStatusMsg(payload.data);
              } else if (payload.type === 'token') {
                streamed += payload.data;
                setStreamingMsg(streamed);
              } else if (payload.type === 'done') {
                doneChat = payload.data.chat;
                setSuggestions(
                  payload.data.no_results
                    ? FOLLOW_UPS_NO_RESULTS
                    : FOLLOW_UPS[payload.data.tool] || []
                );
                finished = true;
              } else if (payload.type === 'error') {
                streamError = payload.data?.error || 'An error occurred. Please try again.';
                finished = true;
              }
            }
          }
        } catch {
        }
        if (!finished && ++retries > MAX_STREAM_RETRIES) {
          streamError = 'Lost connection. Please try again.';
          break;
        }
      }

      if (doneChat) {
        onChatUpdated(chatId, doneChat);
      } else {
        const existingChat = chats[chatId] || {};
        onChatUpdated(chatId, {
          ...existingChat,
          messages: [
            ...(existingChat.messages || []),
            { role: 'user', content: userQuery },
            { role: 'assistant', content: streamError || 'An error occurred. Please try again.' },
          ],
        });
      }
    } catch (err) {
      console.error('Chat error:', err);
      if (currentChatId) {
        const existingChat = chats[currentChatId] || {};
        onChatUpdated(currentChatId, {
          ...existingChat,
          messages: [
            ...messages,
            { role: 'user', content: userQuery },
            { role: 'assistant', content: 'An error occurred. Please try again.' },
          ],
        });
      }
    } finally {
      setLoading(false);
      setOptimisticMsg(null);
      setStatusMsg('');
      setStreamingMsg('');
      onCreditsRefresh?.();
    }
  };

  return (
    <div className="chat-main">
      <div className="chat-header">
        <h2 style={{ fontSize: '1.2rem', fontWeight: '600' }}>
          🛒 Ecommerce Assistant
        </h2>
        <div className="chat-header-meta">
          <span className="chat-header-provider">Powered by Gemini</span>
          {credits && (
            <div
              className="credits-badge"
              title={`${credits.remaining} of ${credits.cap} daily message credits left. 1 credit = 1 message (your question + the AI's reply). Resets at midnight.`}
            >
              <Zap size={13} className={credits.remaining === 0 ? 'credits-empty' : ''} />
              <span>
                <strong>{credits.remaining}</strong> / {credits.cap} credits left today
              </span>
            </div>
          )}
        </div>
      </div>

      <div className="chat-messages" ref={scrollRef}>
        {messages.length === 0 && !optimisticMsg ? (
          <div className="empty-state">
            <ShoppingBag className="empty-icon" />
            <h3 style={{ marginBottom: '8px', color: 'var(--l-ink)' }}>How can I help you today?</h3>
            <p style={{ maxWidth: '400px', fontSize: '0.9rem' }}>
              Ask me about products, pricing, or our store policies. I'm here to assist your shopping experience!
            </p>
          </div>
        ) : (
          <>
            {messages.map((m, idx) => (
              <div key={idx} className={`message ${m.role === 'user' ? 'user' : 'bot'}`}>
                {m.role === 'user' ? (
                  renderUserContent(m.content)
                ) : (
                  <ReactMarkdown components={markdownComponents}>
                    {m.content}
                  </ReactMarkdown>
                )}
              </div>
            ))}

            {optimisticMsg && (
              <div className="message user">{renderUserContent(optimisticMsg)}</div>
            )}

            {loading && streamingMsg && (
              <div className="message bot">
                <ReactMarkdown components={markdownComponents}>
                  {streamingMsg}
                </ReactMarkdown>
              </div>
            )}

            {loading && !streamingMsg && (
              <div className="message bot">
                <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                  {statusMsg && (
                    <div style={{
                      fontSize: '0.82rem',
                      color: 'var(--accent-color)',
                      fontStyle: 'italic',
                      display: 'flex',
                      alignItems: 'center',
                      gap: '7px',
                      animation: 'fadeIn 0.4s ease'
                    }}>
                      <span className="reasoning-dot"></span>
                      {statusMsg}
                    </div>
                  )}
                  <div className="loader">
                    <div className="dot"></div>
                    <div className="dot"></div>
                    <div className="dot"></div>
                  </div>
                </div>
              </div>
            )}

            {!loading && suggestions.length > 0 && (
              <div className="suggestions">
                {suggestions.map((s) => (
                  <button
                    key={s}
                    type="button"
                    className="suggestion-chip"
                    onClick={(e) => handleSend(e, s)}
                  >
                    {s}
                  </button>
                ))}
              </div>
            )}
          </>
        )}
      </div>

      <div className="chat-input-container">
        {outOfCredits && (
          <div className="credit-notice">
            You've used all {credits.cap} of today's message credits. They reset at midnight.
          </div>
        )}
        <form onSubmit={handleSend} className={`input-wrapper${image ? ' has-image' : ''}`}>
          <input ref={fileRef} type="file" accept="image/*" hidden onChange={handleImagePick} />
          {image && (
            <div className="input-image-preview">
              {image.thumb
                ? <img src={image.thumb} alt="preview" />
                : <div className="input-image-placeholder"><ImagePlus size={20} /></div>}
              <button
                type="button"
                className="input-image-remove"
                onClick={() => setImage(null)}
                aria-label="Remove image"
              >
                <X size={12} />
              </button>
            </div>
          )}
          <button
            type="button"
            className="image-btn"
            title="Search by shoe photo"
            aria-label="Search by shoe photo"
            disabled={outOfCredits}
            onClick={() => fileRef.current?.click()}
          >
            <ImagePlus size={18} />
          </button>
          <textarea
            className="chat-input"
            placeholder={outOfCredits ? 'Daily message limit reached — resets at midnight' : 'Type your message here...'}
            rows="1"
            value={input}
            disabled={outOfCredits}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                handleSend(e);
              }
            }}
          />
          <button type="submit" className="send-btn" disabled={loading || (!input.trim() && !image) || outOfCredits}>
            <Send size={18} />
          </button>
        </form>
      </div>
    </div>
  );
};

export default ChatArea;
