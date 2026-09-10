import React, { useState } from 'react';
import { createPortal } from 'react-dom';
import { Plus, MessageSquare, LogOut, Search, X, Pencil, Trash2, Heart, TrendingDown, TrendingUp, ShoppingCart, Package, PanelLeftClose, PanelLeftOpen, Settings } from 'lucide-react';

const PAGE_SIZE = 10;

const Sidebar = ({
  isOpen = true,
  onToggleSidebar,
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
  cartItems = [],
  cartTotal = 0,
  orders = [],
  onRemoveFromCart,
  onPlaceOrder,
  onCancelOrder,
  preferences = '',
  onSavePreferences,
}) => {
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE);
  const [editingId, setEditingId] = useState(null);
  const [editingTitle, setEditingTitle] = useState('');
  const [tab, setTab] = useState('chats');
  // { message, confirmLabel, onConfirm } — drives the styled confirm modal below.
  const [confirmBox, setConfirmBox] = useState(null);
  const [prefsOpen, setPrefsOpen] = useState(false);
  const [prefsDraft, setPrefsDraft] = useState('');

  const openPrefs = () => { setPrefsDraft(preferences || ''); setPrefsOpen(true); };
  const savePrefs = () => { onSavePreferences?.(prefsDraft.trim()); setPrefsOpen(false); };

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
    setConfirmBox({
      message: `Delete "${chat.title}"? This cannot be undone.`,
      confirmLabel: 'Delete',
      onConfirm: () => onDeleteChat(chat.id),
    });
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

  // Collapsed: a thin rail with just expand (top) and logout (bottom).
  if (!isOpen) {
    return (
      <div className="sidebar collapsed">
        <div className="sidebar-rail-top">
          <button
            className="sidebar-collapse-btn"
            onClick={onToggleSidebar}
            title="Show sidebar"
            aria-label="Show sidebar"
          >
            <PanelLeftOpen size={18} />
          </button>
        </div>
        <div className="sidebar-rail-bottom">
          <button className="logout-btn" onClick={onLogout} title="Logout" aria-label="Logout">
            <LogOut size={16} />
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="sidebar">
      <div className="sidebar-header">
        <button className="new-chat-btn" onClick={onNewChat}>
          <Plus size={18} />
          New Chat
        </button>
        <button
          className="sidebar-collapse-btn"
          onClick={onToggleSidebar}
          title="Hide sidebar"
          aria-label="Hide sidebar"
        >
          <PanelLeftClose size={18} />
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
        <button
          className={`sidebar-tab${tab === 'cart' ? ' active' : ''}`}
          onClick={() => setTab('cart')}
        >
          <ShoppingCart size={14} /> Cart{cartItems.length ? ` (${cartItems.length})` : ''}
        </button>
        <button
          className={`sidebar-tab${tab === 'orders' ? ' active' : ''}`}
          onClick={() => setTab('orders')}
        >
          <Package size={14} /> Orders{orders.length ? ` (${orders.length})` : ''}
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
      ) : tab === 'cart' ? (
        <div className="chat-history">
          {cartItems.length === 0 ? (
            <div className="sidebar-empty">
              Your cart is empty. Tap the <ShoppingCart size={12} style={{ verticalAlign: 'middle' }} /> next
              to any product in a chat to add it here.
            </div>
          ) : (
            <>
              {cartItems.map(item => (
                <div className="saved-item" key={item.pid}>
                  <div className="saved-item-head">
                    <a href={item.product_link} target="_blank" rel="noopener noreferrer" className="saved-item-title">
                      {item.title || item.pid}
                    </a>
                    <button
                      className="saved-remove"
                      title="Remove from cart"
                      aria-label="Remove from cart"
                      onClick={() => onRemoveFromCart(item.pid)}
                    >
                      <X size={13} />
                    </button>
                  </div>
                  <div className="saved-item-meta">
                    <span className="saved-price">Rs. {item.price ?? '—'}</span>
                    {item.availability && item.availability !== 'InStock' && (
                      <span className="stock-warn">{item.availability}</span>
                    )}
                  </div>
                </div>
              ))}
              <div className="cart-summary">
                <div className="cart-total"><span>Total</span><strong>Rs. {cartTotal}</strong></div>
                <button
                  className="place-order-btn"
                  onClick={() => setConfirmBox({
                    message: `Place this order for Rs. ${cartTotal}? This is a demo order — no real payment is taken.`,
                    confirmLabel: 'Place order',
                    onConfirm: onPlaceOrder,
                  })}
                >
                  Place order
                </button>
                <p className="demo-note">Demo order · simulated · cash on delivery</p>
              </div>
            </>
          )}
        </div>
      ) : tab === 'orders' ? (
        <div className="chat-history">
          {orders.length === 0 ? (
            <div className="sidebar-empty">
              No orders yet. Add items to your cart, then place an order to see it here.
            </div>
          ) : (
            orders.map(o => (
              <div className="saved-item" key={o.id}>
                <div className="saved-item-head">
                  <span className="saved-item-title">Order #{o.id}</span>
                  <span className={`order-status ${o.status}`}>{o.status}</span>
                </div>
                <div className="order-items">
                  {o.items.map((it, i) => (
                    <div className="order-line" key={i}>
                      {it.title}{it.quantity > 1 ? ` × ${it.quantity}` : ''} — Rs. {it.price}
                    </div>
                  ))}
                </div>
                <div className="saved-item-meta">
                  <span className="saved-price">Total Rs. {o.total}</span>
                  {o.status === 'placed' && (
                    <button
                      className="cancel-order-btn"
                      onClick={() => setConfirmBox({
                        message: `Cancel order #${o.id}? This can't be undone.`,
                        confirmLabel: 'Cancel order',
                        onConfirm: () => onCancelOrder(o.id),
                      })}
                    >
                      Cancel
                    </button>
                  )}
                </div>
              </div>
            ))
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
        <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
          <button className="logout-btn" onClick={openPrefs} title="Shopping preferences" aria-label="Shopping preferences">
            <Settings size={16} />
          </button>
          <button className="logout-btn" onClick={onLogout} title="Logout">
            <LogOut size={16} />
          </button>
        </div>
      </div>

      {prefsOpen && createPortal(
        <div className="modal-overlay" onClick={() => setPrefsOpen(false)}>
          <div className="modal-card" onClick={(e) => e.stopPropagation()}>
            <h3 className="modal-title">Shopping preferences</h3>
            <p className="modal-sub">
              Saved across sessions and applied to your product searches — e.g. favourite brands or a budget.
            </p>
            <textarea
              className="prefs-textarea"
              value={prefsDraft}
              maxLength={500}
              placeholder="e.g. Prefers Puma and Nike; budget under 3000; men's shoes"
              onChange={(e) => setPrefsDraft(e.target.value)}
            />
            <div className="modal-actions">
              <button className="modal-btn-ghost" onClick={() => { setPrefsDraft(''); }}>
                Clear
              </button>
              <button className="modal-btn-primary" onClick={savePrefs}>
                Save
              </button>
            </div>
          </div>
        </div>,
        document.body,
      )}

      {confirmBox && createPortal(
        <div className="modal-overlay" onClick={() => setConfirmBox(null)}>
          <div className="modal-card" onClick={(e) => e.stopPropagation()}>
            <p className="modal-msg">{confirmBox.message}</p>
            <div className="modal-actions">
              <button className="modal-btn-ghost" onClick={() => setConfirmBox(null)}>
                Keep
              </button>
              <button
                className="modal-btn-primary"
                onClick={() => { confirmBox.onConfirm(); setConfirmBox(null); }}
              >
                {confirmBox.confirmLabel}
              </button>
            </div>
          </div>
        </div>,
        document.body,
      )}
    </div>
  );
};

export default Sidebar;
