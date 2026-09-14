import React, { useState } from 'react';
import { Plus, MessageSquare, LogOut, Search, X, Pencil, Trash2, Heart, TrendingDown, TrendingUp } from 'lucide-react';

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
  onDeleteChat,
  onRenameChat,
  savedItems = [],
  onUnsave,
}) => {
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE);
  const [editingId, setEditingId] = useState(null);
  const [editingTitle, setEditingTitle] = useState('');
  const [tab, setTab] = useState('chats');

  const startRename = (chat) => {
    setEditingId(chat.id);
    setEditingTitle(chat.title);
  };

  const cancelRename = () => {
    setEditingId(null);
    setEditingTitle('');
  };

  const commitRename = (chatId) => {
    const title = editingTitle.trim();
    if (title) onRenameChat(chatId, title);
    setEditingId(null);
    setEditingTitle('');
  };

  const handleDelete = (chat) => {
    if (window.confirm(`Delete "${chat.title}"? This cannot be undone.`)) {
      onDeleteChat(chat.id);
    }
  };

  // Price-drop alert: saved items now cheaper than when they were saved.
  const drops = savedItems.filter(s => s.price_change < 0);

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

      <div className="sidebar-tabs">
        <button
          className={`sidebar-tab${tab === 'chats' ? ' active' : ''}`}
          onClick={() => setTab('chats')}
        >
          <MessageSquare size={14} /> Chats
        </button>
        <button
          className={`sidebar-tab${tab === 'saved' ? ' active' : ''}`}
          onClick={() => setTab('saved')}
        >
          <Heart size={14} /> Saved{savedItems.length ? ` (${savedItems.length})` : ''}
          {drops.length > 0 && (
            <span className="drop-badge" title={`${drops.length} price drop${drops.length > 1 ? 's' : ''}`}>
              <TrendingDown size={11} />{drops.length}
            </span>
          )}
        </button>
      </div>

      {tab === 'saved' ? (
        <div className="chat-history">
          {drops.length > 0 && (
            <div className="drop-summary">
              <TrendingDown size={13} />
              {drops.length === 1
                ? `1 saved product dropped Rs. ${Math.abs(drops[0].price_change)}`
                : `${drops.length} saved products dropped in price`}
            </div>
          )}
          {savedItems.length === 0 ? (
            <div className="sidebar-empty">
              Nothing saved yet. Tap the <Heart size={12} style={{ verticalAlign: 'middle' }} /> next
              to any product in a chat to save it here.
            </div>
          ) : (
            savedItems.map(item => {
              const drop = item.price_change;
              return (
                <div className="saved-item" key={item.pid}>
                  <div className="saved-item-head">
                    <a href={item.product_link} target="_blank" rel="noopener noreferrer" className="saved-item-title">
                      {item.title || item.pid}
                    </a>
                    <button
                      className="saved-remove"
                      title="Remove from saved"
                      aria-label="Remove from saved"
                      onClick={() => onUnsave(item.pid)}
                    >
                      <X size={13} />
                    </button>
                  </div>
                  <div className="saved-item-meta">
                    <span className="saved-price">Rs. {item.price ?? '—'}</span>
                    {drop < 0 && (
                      <span className="price-down">
                        <TrendingDown size={12} /> {Math.abs(drop)} since saved
                      </span>
                    )}
                    {drop > 0 && (
                      <span className="price-up">
                        <TrendingUp size={12} /> {drop} since saved
                      </span>
                    )}
                    {item.availability && item.availability !== 'InStock' && (
                      <span className="stock-warn">{item.availability}</span>
                    )}
                  </div>
                </div>
              );
            })
          )}
        </div>
      ) : (
      <>
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
            onClick={() => { if (editingId !== chat.id) onSelectChat(chat.id); }}
          >
            <MessageSquare size={16} style={{ flexShrink: 0 }} />
            {editingId === chat.id ? (
              <input
                className="chat-rename-input"
                autoFocus
                value={editingTitle}
                maxLength={60}
                onChange={(e) => setEditingTitle(e.target.value)}
                onClick={(e) => e.stopPropagation()}
                onBlur={cancelRename}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') { e.preventDefault(); commitRename(chat.id); }
                  if (e.key === 'Escape') { e.preventDefault(); cancelRename(); }
                }}
              />
            ) : (
              <>
                <span className="chat-item-title" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {chat.title}
                </span>
                <div className="chat-item-actions">
                  <Pencil size={13} title="Rename" onClick={(e) => { e.stopPropagation(); startRename(chat); }} />
                  <Trash2 size={13} title="Delete" onClick={(e) => { e.stopPropagation(); handleDelete(chat); }} />
                </div>
              </>
            )}
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
      </>
      )}

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
