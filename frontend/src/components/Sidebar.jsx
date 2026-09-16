import React, { useState } from 'react';
import { Plus, MessageSquare, LogOut, Search, X, Pencil, Trash2, Heart,
         TrendingDown, TrendingUp, ShoppingCart, Package,
         PanelLeftClose, PanelLeftOpen } from 'lucide-react';
import ConfirmModal from './ConfirmModal';

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
  cartItems = [],
  cartTotal = 0,
  onRemoveFromCart,
  orders = [],
  onPlaceOrder,
  onCancelOrder,
  onToggleOpen,
  isOpen = true,
}) => {
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE);
  const [editingId, setEditingId] = useState(null);
  const [editingTitle, setEditingTitle] = useState('');
  const [tab, setTab] = useState('chats');
  // One descriptor for the one modal, rather than a boolean per action. Adding
  // a fourth confirmable action should not mean a fourth piece of state.
  const [confirm, setConfirm] = useState(null);   // { title, message, confirmLabel, danger, run }
  const [busy, setBusy] = useState(false);
  const [orderError, setOrderError] = useState('');

  const runConfirm = async () => {
    if (!confirm) return;
    setBusy(true);
    try {
      await confirm.run();
      setConfirm(null);
      setOrderError('');
    } catch (err) {
      // Surface the server's reason -- "no longer in stock: X" is the whole
      // point of the 409 and is useless if swallowed.
      setOrderError(err?.response?.data?.detail || 'Something went wrong. Try again.');
      setConfirm(null);
    } finally {
      setBusy(false);
    }
  };

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
    setConfirm({
      title: 'Delete this chat?',
      message: `"${chat.title}" and its messages will be removed. This cannot be undone.`,
      confirmLabel: 'Delete',
      danger: true,
      run: async () => onDeleteChat(chat.id),
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

  return (
    <div className={`sidebar${isOpen ? '' : ' collapsed'}`}>
      <div className="sidebar-header">
        <button className="new-chat-btn" onClick={onNewChat}>
          <Plus size={18} />
          New Chat
        </button>
        <button
          className="sidebar-collapse-btn"
          onClick={onToggleOpen}
          title={isOpen ? 'Hide sidebar' : 'Show sidebar'}
          aria-label={isOpen ? 'Hide sidebar' : 'Show sidebar'}
          aria-expanded={isOpen}
        >
          {isOpen ? <PanelLeftClose size={16} /> : <PanelLeftOpen size={16} />}
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
          <Heart size={14} />
          <span className="tab-label">Saved{savedItems.length ? ` (${savedItems.length})` : ''}</span>
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
          <ShoppingCart size={14} />
          <span className="tab-label">Cart{cartItems.length ? ` (${cartItems.length})` : ''}</span>
        </button>
        <button
          className={`sidebar-tab${tab === 'orders' ? ' active' : ''}`}
          onClick={() => setTab('orders')}
        >
          <Package size={14} />
          <span className="tab-label">Orders</span>
        </button>
      </div>

      {tab === 'orders' ? (
        <div className="chat-history">
          {orderError && <div className="cart-error">{orderError}</div>}
          {orders.length === 0 ? (
            <div className="sidebar-empty">
              No orders yet. Add items to your cart, then place an order to see it here.
            </div>
          ) : (
            <div className="orders-section">
              <div className="orders-heading">Your orders</div>
              {orders.map(o => (
                <div className={`order-card${o.status === 'cancelled' ? ' cancelled' : ''}`} key={o.id}>
                  <div className="order-card-head">
                    <span className="order-id">Order #{o.id}</span>
                    <span className={`order-status order-status-${o.status}`}>{o.status}</span>
                  </div>
                  <div className="order-lines">
                    {o.items.map(i => (
                      <div className="order-line" key={i.pid + '-' + i.price}>
                        <span className="order-line-title">{i.title || i.pid}</span>
                        <span>Rs. {i.price}</span>
                      </div>
                    ))}
                  </div>
                  <div className="order-card-foot">
                    <strong>Rs. {o.total}</strong>
                    {o.status === 'placed' && (
                      <button
                        className="order-cancel-btn"
                        onClick={() => setConfirm({
                          title: 'Cancel this order?',
                          message: `Order #${o.id} for Rs. ${o.total} will be cancelled. This cannot be undone.`,
                          confirmLabel: 'Cancel order',
                          cancelLabel: 'Keep it',
                          danger: true,
                          run: () => onCancelOrder(o.id),
                        })}
                      >
                        Cancel
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      ) : tab === 'cart' ? (
        <div className="chat-history">
          {orderError && (
            <div className="cart-error">{orderError}</div>
          )}

          {cartItems.length === 0 ? (
            <div className="sidebar-empty">
              Your cart is empty. Tap the <ShoppingCart size={12} style={{ verticalAlign: 'middle' }} /> next
              to a product in chat to add it.
            </div>
          ) : (
            <>
              {cartItems.map(item => (
                <div className="saved-item" key={item.pid}>
                  <div className="saved-item-head">
                    <a
                      className="saved-item-title"
                      href={item.product_link || '#'}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      {item.title || item.pid}
                    </a>
                    <button
                      className="saved-remove"
                      onClick={() => onRemoveFromCart(item.pid)}
                      title="Remove from cart"
                      aria-label="Remove from cart"
                    >
                      <X size={14} />
                    </button>
                  </div>
                  <div className="saved-item-meta">
                    <span className="saved-price">Rs. {item.price ?? '—'}</span>
                    {item.availability && item.availability !== 'InStock' && (
                      <span className="stock-warn">{item.availability}</span>
                    )}
                    {/* The "+" is permanently disabled, not a placeholder. The
                        catalogue stores availability as a three-value string and
                        carries no unit count, so there is no number to check a
                        larger quantity against. Showing a working stepper would
                        be inventing stock we cannot see. */}
                    <span className="qty-stepper">
                      <button className="qty-btn" disabled aria-hidden="true">−</button>
                      <span className="qty-value">{item.quantity}</span>
                      <button
                        className="qty-btn"
                        disabled
                        title="The catalogue doesn't publish stock counts, so quantity is fixed at 1"
                      >
                        +
                      </button>
                    </span>
                  </div>
                </div>
              ))}

              <div className="cart-total">
                <span>Total</span>
                <strong>Rs. {cartTotal}</strong>
              </div>
              <button
                className="cart-order-btn"
                onClick={() => setConfirm({
                  title: 'Place this order?',
                  message: `${cartItems.length} item${cartItems.length > 1 ? 's' : ''} for Rs. ${cartTotal}. Your cart will be emptied.`,
                  confirmLabel: 'Place order',
                  run: onPlaceOrder,
                })}
              >
                <Package size={15} /> Place order
              </button>
              {/* Says plainly what this is. There is no payment step and no
                  fulfilment behind it, and a checkout that stays quiet about
                  that is the kind of thing a shopper only discovers later. */}
              <div className="cart-demo-note">Demo order · simulated · cash on delivery</div>
            </>
          )}

        </div>
      ) : tab === 'saved' ? (
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

      <ConfirmModal
        open={!!confirm}
        title={confirm?.title}
        message={confirm?.message}
        confirmLabel={confirm?.confirmLabel}
        cancelLabel={confirm?.cancelLabel}
        danger={confirm?.danger}
        busy={busy}
        onConfirm={runConfirm}
        onCancel={() => setConfirm(null)}
      />

      <div className="sidebar-footer">
        <div className="sidebar-user">
          <span className="sidebar-user-dot" />
          <span className="sidebar-user-name">{username}</span>
        </div>
        <button className="logout-btn" onClick={onLogout} title="Logout">
          <LogOut size={16} />
        </button>
      </div>
    </div>
  );
};

export default Sidebar;
