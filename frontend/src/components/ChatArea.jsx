import React, { useState, useEffect, useRef } from 'react';
import { Send, ShoppingBag, Heart } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import api from '../api';

// Answers are markdown, not structured data, but every product link carries a
// pid — enough to hang a save button off the link.
const PID_RE = /[?&]pid=([A-Za-z0-9]+)/;

const forceLogout = () => {
  localStorage.removeItem('token');
  localStorage.removeItem('user');
  localStorage.removeItem('login_time');
  window.location.reload();
};

// Follow-up chips, keyed by the tool that answered. A lookup, not an LLM call,
// and only queries the agent actually supports.
// The answer lives in the job's event log on the server, so a dropped
// connection is recoverable: reconnect and resume from the last seq seen.
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
  compare_saved_products: [
    'Any cheaper alternatives?',
    'Which has the most ratings?',
  ],
};

// When a search came back empty or was refused (colour/size, out-of-catalogue),
// repeating the same dead end helps nobody — steer to searches that do work.
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
  credits,
  onCreditsRefresh,
}) => {
  const outOfCredits = credits && credits.remaining <= 0;
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [optimisticMsg, setOptimisticMsg] = useState(null);
  const [statusMsg, setStatusMsg] = useState('');
  const [streamingMsg, setStreamingMsg] = useState('');
  // Follow-ups for the latest answer only. Kept in state (not persisted) so they
  // vanish on refresh rather than trailing every historical message.
  const [suggestions, setSuggestions] = useState([]);
  const scrollRef = useRef(null);

  // Shared by the persisted-message and live-streaming renderers.
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
        </>
      );
    },
  };

  // Scroll to bottom as content grows
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, loading, optimisticMsg, streamingMsg]);

  // `preset` lets a follow-up chip send its text directly, bypassing the input box.
  const handleSend = async (e, preset) => {
    e?.preventDefault();
    const userQuery = (preset ?? input).trim();
    if (!userQuery || loading || outOfCredits) return;

    setInput('');
    setLoading(true);
    setStatusMsg('');
    setStreamingMsg('');
    setSuggestions([]);          // the previous answer's follow-ups no longer apply
    setOptimisticMsg(userQuery);

    const history = messages.slice(-5);

    try {
      let chatId = currentChatId;

      // If no chat selected, create one first
      if (!chatId) {
        const newChatRes = await api.post('/chats/new');
        chatId = newChatRes.data.chat_id;
        onNewChatCreated(chatId, newChatRes.data.chat);
      }

      // Stream the answer via SSE. We use fetch (not axios) so we can read
      // response.body as it arrives.
      const token = localStorage.getItem('token');
      const res = await fetch(`${api.defaults.baseURL}/chats/${chatId}/message`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          query: userQuery,
          history,
        }),
      });

      if (res.status === 401) {
        forceLogout();
        return;
      }
      if (res.status === 429) {
        // Out of daily credits (or per-minute rate limit). Show the reason and
        // refresh the badge; the message wasn't processed, so no credit was spent.
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

      // The answer is produced by a worker, so this returns a job id, not the
      // answer. The job outlives this request: closing the tab no longer
      // destroys it.
      const { job_id: jobId } = await res.json();

      let streamed = '';
      let doneChat = null;
      let streamError = null;
      let seq = 0;          // resume cursor — the last event we actually saw
      let finished = false;
      let retries = 0;

      // Tail the job's durable event log. The worker appends events whether or
      // not anyone is listening, so a dropped connection costs a reconnect and
      // nothing else: we ask for everything after `seq` and carry on.
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

            // SSE events are separated by a blank line
            const events = buffer.split('\n\n');
            buffer = events.pop(); // keep the trailing incomplete chunk

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
          // Connection dropped mid-answer. `seq` already points at the last
          // event we processed, so the next attempt resumes rather than restarts.
        }
        if (!finished && ++retries > MAX_STREAM_RETRIES) {
          streamError = 'Lost connection. Please try again.';
          break;
        }
      }

      if (doneChat) {
        onChatUpdated(chatId, doneChat);
      } else {
        // No saved chat returned — show the error locally (not persisted server-side)
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
      onCreditsRefresh?.();   // a message just spent a credit — count the badge down
    }
  };

  return (
    <div className="chat-main">
      <div className="chat-header">
        <h2 style={{ fontSize: '1.2rem', fontWeight: '600' }}>
          🛒 Ecommerce Assistant
        </h2>
        <div style={{ color: 'var(--text-secondary)', fontSize: '0.8rem' }}>
          Powered by Gemini
        </div>
      </div>

      <div className="chat-messages" ref={scrollRef}>
        {messages.length === 0 && !optimisticMsg ? (
          <div className="empty-state">
            <ShoppingBag className="empty-icon" />
            <h3 style={{ marginBottom: '8px', color: 'white' }}>How can I help you today?</h3>
            <p style={{ maxWidth: '400px', fontSize: '0.9rem' }}>
              Ask me about products, pricing, or our store policies. I'm here to assist your shopping experience!
            </p>
          </div>
        ) : (
          <>
            {messages.map((m, idx) => (
              <div key={idx} className={`message ${m.role === 'user' ? 'user' : 'bot'}`}>
                {m.role === 'user' ? (
                  m.content
                ) : (
                  <ReactMarkdown components={markdownComponents}>
                    {m.content}
                  </ReactMarkdown>
                )}
              </div>
            ))}

            {/* Optimistic user message while waiting */}
            {optimisticMsg && (
              <div className="message user">{optimisticMsg}</div>
            )}

            {/* Live streaming answer */}
            {loading && streamingMsg && (
              <div className="message bot">
                <ReactMarkdown components={markdownComponents}>
                  {streamingMsg}
                </ReactMarkdown>
              </div>
            )}

            {/* Progress + loader before the first token arrives */}
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

            {/* Follow-ups for the latest answer — surfaces capabilities (relative
                comparisons, compare-saved) that a blank input box hides. */}
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
        <form onSubmit={handleSend} className="input-wrapper">
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
          <button type="submit" className="send-btn" disabled={loading || !input.trim() || outOfCredits}>
            <Send size={18} />
          </button>
        </form>
      </div>
    </div>
  );
};

export default ChatArea;
