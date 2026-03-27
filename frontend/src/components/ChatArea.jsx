import React, { useState, useEffect, useRef } from 'react';
import { Send, ShoppingBag } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import api from '../api';

const reasoningSteps = [
  'Understanding your query...',
  'Routing to the right tool...',
  'Searching the knowledge base...',
  'Analyzing results...',
  'Generating response...',
];

const ChatArea = ({
  user,
  currentChatId,
  chats,
  messages,
  onChatUpdated,
  onNewChatCreated,
  geminiKey,
}) => {
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [optimisticMsg, setOptimisticMsg] = useState(null);
  const [statusMsg, setStatusMsg] = useState('');
  const scrollRef = useRef(null);

  // Scroll to bottom when new messages arrive
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, loading, optimisticMsg]);

  // Cycle through reasoning steps while loading
  useEffect(() => {
    if (!loading) {
      setStatusMsg('');
      return;
    }
    let stepIndex = 0;
    setStatusMsg(reasoningSteps[0]);
    const interval = setInterval(() => {
      stepIndex = (stepIndex + 1) % reasoningSteps.length;
      setStatusMsg(reasoningSteps[stepIndex]);
    }, 2400);
    return () => clearInterval(interval);
  }, [loading]);

  const handleSend = async (e) => {
    e.preventDefault();
    if (!input.trim() || loading) return;

    const userQuery = input.trim();
    setInput('');
    setLoading(true);
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

      const response = await api.post(`/chats/${chatId}/message`, {
        query: userQuery,
        history,
        gemini_api_key: geminiKey || null,
      });

      onChatUpdated(chatId, response.data.chat);
    } catch (err) {
      console.error('Chat error:', err);

      if (err.response && err.response.status === 401) {
        localStorage.removeItem('token');
        localStorage.removeItem('user');
        localStorage.removeItem('login_time');
        window.location.reload();
        return;
      }

      if (currentChatId) {
        const existingChat = chats[currentChatId] || {};
        onChatUpdated(currentChatId, {
          ...existingChat,
          messages: [...messages, { role: 'user', content: userQuery }, { role: 'assistant', content: 'An error occurred. Please try again.' }],
        });
      }
    } finally {
      setLoading(false);
      setOptimisticMsg(null);
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
                  <ReactMarkdown
                    components={{
                      a: ({ node, ...props }) => (
                        <a {...props} target="_blank" rel="noopener noreferrer" />
                      ),
                    }}
                  >
                    {m.content}
                  </ReactMarkdown>
                )}
              </div>
            ))}

            {/* Optimistic user message while waiting */}
            {optimisticMsg && (
              <div className="message user">{optimisticMsg}</div>
            )}

            {/* Loading indicator with cycling status */}
            {loading && (
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
