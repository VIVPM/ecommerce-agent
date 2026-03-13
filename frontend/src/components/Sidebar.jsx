import React, { useState } from 'react';
import { Plus, MessageSquare, LogOut, Search, X } from 'lucide-react';

const PAGE_SIZE = 10;

const Sidebar = ({ 
  chats, 
  currentChatId, 
  onSelectChat, 
  onNewChat, 
  onLogout, 
  username,
  searchQuery,
  setSearchQuery,
  geminiApiKey,
  onApiKeyChange
}) => {
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE);
  const [localApiKey, setLocalApiKey] = useState(geminiApiKey || '');
  const [keySaved, setKeySaved] = useState(false);

  const handleSaveKey = () => {
    onApiKeyChange(localApiKey);
    setKeySaved(true);
    setTimeout(() => setKeySaved(false), 2000);
  };

  const filteredChats = Object.values(chats)
    .filter(chat => chat.messages && chat.messages.length > 0)
    .filter(chat => 
      chat.title.toLowerCase().includes(searchQuery.toLowerCase()) ||
      chat.messages.some(m => m.content.toLowerCase().includes(searchQuery.toLowerCase()))
    )
    .sort((a, b) => new Date(b.updated_at) - new Date(a.updated_at));

  const visibleChats = filteredChats.slice(0, visibleCount);
  const hasMore = filteredChats.length > visibleCount;

  return (
    <div className="sidebar">
      <div className="sidebar-header">
        <button className="new-chat-btn" onClick={onNewChat}>
          <Plus size={18} />
          New Chat
        </button>
      </div>

      <div style={{ padding: '1.2rem 1.5rem 1rem' }}>
        <div style={{ marginBottom: '8px', fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
          Gemini API Key
        </div>
        <div style={{ display: 'flex', gap: '6px', alignItems: 'center', marginBottom: '8px' }}>
          <div className="input-wrapper" style={{ borderRadius: '12px', flex: 1, marginBottom: 0 }}>
            <input 
              type="password" 
              className="chat-input" 
              style={{ padding: '0.6rem 1rem', fontSize: '0.85rem' }}
              placeholder="Enter Gemini Key..."
              value={localApiKey}
              onChange={(e) => setLocalApiKey(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') handleSaveKey(); }}
            />
          </div>
          <button
            onClick={handleSaveKey}
            style={{
              padding: '0.55rem 0.85rem',
              background: keySaved ? 'var(--success-color)' : 'var(--accent-color)',
              color: 'white',
              border: 'none',
              borderRadius: '12px',
              fontSize: '0.78rem',
              fontWeight: 600,
              cursor: 'pointer',
              whiteSpace: 'nowrap',
              transition: 'background 0.2s'
            }}
          >
            {keySaved ? '✓ Saved' : 'Save'}
          </button>
        </div>
        <a 
          href="https://aistudio.google.com/app/apikey" 
          target="_blank" 
          rel="noopener noreferrer"
          style={{ fontSize: '0.7rem', color: 'var(--accent-color)', textDecoration: 'none' }}
        >
          Get your Gemini API Key here →
        </a>
      </div>

      <div className="search-container">
        <div className="input-wrapper" style={{ borderRadius: '12px' }}>
          <input 
            type="text" 
            className="chat-input" 
            style={{ padding: '0.6rem 2.5rem 0.6rem 1rem', fontSize: '0.85rem' }}
            placeholder="Search chats..."
            value={searchQuery}
            onChange={(e) => {
              setSearchQuery(e.target.value);
              setVisibleCount(PAGE_SIZE); // reset pagination on search
            }}
          />
          {searchQuery ? (
            <X 
              size={14} 
              className="send-btn" 
              style={{ right: '8px', bottom: '8px', width: '24px', height: '24px', background: 'transparent', color: 'var(--text-secondary)' }} 
              onClick={() => { setSearchQuery(''); setVisibleCount(PAGE_SIZE); }}
            />
          ) : (
            <Search size={14} className="send-btn" style={{ right: '8px', bottom: '8px', width: '24px', height: '24px', background: 'transparent', color: 'var(--text-secondary)' }} />
          )}
        </div>
      </div>

      <div className="chat-history">
        {visibleChats.map(chat => (
          <div 
            key={chat.id} 
            className={`chat-item ${currentChatId === chat.id ? 'active' : ''}`}
            onClick={() => onSelectChat(chat.id)}
          >
            <MessageSquare size={16} />
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {chat.title}
            </span>
          </div>
        ))}

        {/* Load more chats button */}
        {hasMore && (
          <div style={{ padding: '0.5rem 0.5rem 1rem', textAlign: 'center' }}>
            <button
              className="load-more-btn"
              onClick={() => setVisibleCount(prev => prev + PAGE_SIZE)}
            >
              Load {Math.min(PAGE_SIZE, filteredChats.length - visibleCount)} more chats
            </button>
          </div>
        )}

        {filteredChats.length === 0 && (
          <div style={{ padding: '2rem', textAlign: 'center', color: 'var(--text-secondary)', fontSize: '0.85rem' }}>
            No chats found
          </div>
        )}
      </div>

      <div className="sidebar-footer">
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <div style={{ width: '8px', height: '8px', borderRadius: '50%', background: 'var(--success-color)' }}></div>
          <span>{username}</span>
        </div>
        <button className="logout-btn" onClick={onLogout} title="Logout">
          <LogOut size={16} />
        </button>
      </div>
    </div>
  );
};

export default Sidebar;
