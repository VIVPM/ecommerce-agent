import React, { useState, useEffect, useRef } from 'react';
import { Send, ShoppingBag, Heart } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import api from '../api';

// Answers come back as markdown text, not structured data — but every product
// link carries Flipkart's pid, so we can hang a save button off the link itself
// without changing the response format.
const PID_RE = /[?&]pid=([A-Za-z0-9]+)/;

const forceLogout = () => {
  localStorage.removeItem('token');
  localStorage.removeItem('user');
  localStorage.removeItem('login_time');
  window.location.reload();
};

const ChatArea = ({
  user,
  currentChatId,
  chats,
  messages,
  onChatUpdated,
  onNewChatCreated,
  savedPids,
  onToggleSave,
}) => {
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [optimisticMsg, setOptimisticMsg] = useState(null);
  const [statusMsg, setStatusMsg] = useState('');
  const [streamingMsg, setStreamingMsg] = useState('');
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

  const handleSend = async (e) => {
    e.preventDefault();
    if (!input.trim() || loading) return;

    const userQuery = input.trim();
    setInput('');
    setLoading(true);
    setStatusMsg('');
    setStreamingMsg('');
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
      if (!res.ok || !res.body) {
        throw new Error(`Request failed (${res.status})`);
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let streamed = '';
      let doneChat = null;
      let streamError = null;

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
          if (payload.type === 'status') {
            setStatusMsg(payload.data);
          } else if (payload.type === 'token') {
            streamed += payload.data;
            setStreamingMsg(streamed);
          } else if (payload.type === 'done') {
            doneChat = payload.data.chat;
          } else if (payload.type === 'error') {
            streamError = payload.data;
          }
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
          </>
        )}
      </div>

      <div className="chat-input-container">
        <form onSubmit={handleSend} className="input-wrapper">
          <textarea
            className="chat-input"
            placeholder="Type your message here..."
            rows="1"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                handleSend(e);
              }
            }}
          />
          <button type="submit" className="send-btn" disabled={loading || !input.trim()}>
            <Send size={18} />
          </button>
        </form>
      </div>
    </div>
  );
};

export default ChatArea;
