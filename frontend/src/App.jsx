import React, { useState, useEffect, useMemo } from 'react';
import './index.css';
import Auth from './components/Auth';
import LandingPage from './components/LandingPage';
import Sidebar from './components/Sidebar';
import ChatArea from './components/ChatArea';
import api from './api';

const App = () => {
  const [user, setUser] = useState(null);
  const [chats, setChats] = useState({});
  const [currentChatId, setCurrentChatId] = useState(() => localStorage.getItem('currentChatId'));
  const [searchQuery, setSearchQuery] = useState('');
  const [isReady, setIsReady] = useState(false);
  const [showAuth, setShowAuth] = useState(false);
  const [authMode, setAuthMode] = useState('login');
  const [savedItems, setSavedItems] = useState([]);
  const [cartItems, setCartItems] = useState([]);
  const [orders, setOrders] = useState([]);
  const [credits, setCredits] = useState(null); // { cap, used, remaining } — daily message allowance
  const [sidebarOpen, setSidebarOpen] = useState(true);

  // Sets of pids, for O(1) lookup when rendering product links in chat.
  // Memoized on a membership KEY (sorted pids), so a background re-fetch or a
  // price/quantity change that doesn't add/remove a product keeps the same Set
  // identity — that's what stops the chat save/cart icons from re-mounting and
  // flashing on every render.
  const savedPidKey = [...new Set(savedItems.map(s => s.pid))].sort().join('|');
  const cartPidKey = [...new Set(cartItems.map(c => c.pid))].sort().join('|');
  const savedPids = useMemo(() => new Set(savedPidKey ? savedPidKey.split('|') : []), [savedPidKey]);
  const cartPids = useMemo(() => new Set(cartPidKey ? cartPidKey.split('|') : []), [cartPidKey]);
  // Derived, not fetched — so quantity ± / remove update the total in the same
  // instant as the optimistic item change, with no server round-trip.
  const cartTotal = cartItems.reduce((sum, c) => sum + (c.price || 0) * (c.quantity || 1), 0);

  const loadChats = async (userId) => {
    try {
      const response = await api.get('/chats');
      const freshChats = response.data.chats || {};
      setChats(freshChats);
      // Cache the fresh chats for instant load next refresh
      localStorage.setItem(`chats_${userId}`, JSON.stringify(freshChats));
    } catch (err) {
      console.error('Failed to load chats (server may be waking up):', err);
      // Silently fail — cached chats are already shown
    }
  };

  const loadSaved = async () => {
    try {
      const res = await api.get('/saved');
      setSavedItems(res.data.saved || []);
    } catch (err) {
      console.error('Failed to load saved products:', err);
    }
  };

  const loadCredits = async () => {
    try {
      const res = await api.get('/account/credits');
      setCredits(res.data);
    } catch {
      // Non-critical badge — leave it hidden if the call fails.
    }
  };

  const loadCart = async () => {
    try {
      const res = await api.get('/cart');
      setCartItems(res.data.cart || []);   // total is derived from this
    } catch (err) {
      console.error('Failed to load cart:', err);
    }
  };

  const loadOrders = async () => {
    try {
      const res = await api.get('/orders');
      setOrders(res.data.orders || []);
    } catch (err) {
      console.error('Failed to load orders:', err);
    }
  };

  // All three are optimistic: mutate the cart locally now (so the 🛒 icon /
  // quantity / total move instantly), fire the API, then reconcile via loadCart.
  // One of each product; there is no stock count to support quantities, so the
  // cart is a set of products (qty 1), added/removed by the chat toggle.
  const addToCart = async (pid) => {
    setCartItems(prev => prev.some(c => c.pid === pid) ? prev : [...prev, { pid, quantity: 1 }]);
    try {
      await api.post('/cart', { pid });
      loadCart();
    } catch (err) {
      console.error('Failed to add to cart:', err);
      loadCart();
    }
  };

  const removeFromCart = async (pid) => {
    setCartItems(prev => prev.filter(c => c.pid !== pid));
    try {
      await api.delete(`/cart/${pid}`);   // persist only
    } catch (err) {
      console.error('Failed to remove from cart:', err);
      loadCart();
    }
  };

  // The chat 🛒 button toggles: add if not in the cart, remove if it is.
  const toggleCart = (pid) => cartPids.has(pid) ? removeFromCart(pid) : addToCart(pid);

  const placeOrder = async () => {
    try {
      await api.post('/orders');
      await loadCart();
      await loadOrders();
    } catch (err) {
      console.error('Failed to place order:', err);
    }
  };

  const cancelOrder = async (id) => {
    try {
      await api.post(`/orders/${id}/cancel`);
      await loadOrders();
    } catch (err) {
      console.error('Failed to cancel order:', err);
    }
  };

  // The agent can place/cancel orders from chat too, so refresh both after any
  // order-related answer to keep the sidebar in sync with what the tool did.
  const refreshOrderState = () => { loadCart(); loadOrders(); };

  // Save/unsave from anywhere. Optimistic: flip local state now so the heart
  // reflects instantly, then reconcile with the server (which fills title/price
  // and correct ordering). On error, reload to fall back to the server truth.
  const toggleSave = async (pid) => {
    const isSaved = savedPids.has(pid);
    setSavedItems(prev => isSaved ? prev.filter(s => s.pid !== pid) : [...prev, { pid }]);
    try {
      if (isSaved) {
        await api.delete(`/saved/${pid}`);
      } else {
        await api.post('/saved', { pid });
      }
      loadSaved();
    } catch (err) {
      console.error('Failed to update saved product:', err);
      loadSaved();   // revert optimistic change to server state
    }
  };

  const clearSession = () => {
    setUser(null);
    setChats({});
    setCurrentChatId(null);
    setSavedItems([]);
    setCartItems([]);
    setOrders([]);
    setCredits(null);
    setShowAuth(false);
    localStorage.removeItem('token');
    localStorage.removeItem('user');
    localStorage.removeItem('login_time');
    localStorage.removeItem('currentChatId');
    setIsReady(true);
  };

  // Persistence check on mount. Declared after clearSession/loadChats/loadSaved
  // so those are in scope before this effect references them (react-hooks rule).
  useEffect(() => {
    const storedUser = localStorage.getItem('user');
    const loginTime = localStorage.getItem('login_time');

    if (storedUser && loginTime) {
      const elapsed = Date.now() - parseInt(loginTime);
      // Must match JWT_EXPIRY_HOURS in backend main.py — the shorter of the two wins.
      const SESSION_MS = 12 * 60 * 60 * 1000;

      if (elapsed > SESSION_MS) {
        // Session expired — force logout
        clearSession();
      } else {
        const parsedUser = JSON.parse(storedUser);
        setUser(parsedUser);

        // Immediately show cached chats
        const cachedChats = localStorage.getItem(`chats_${parsedUser.user_id}`);
        if (cachedChats) setChats(JSON.parse(cachedChats));

        // Sync fresh from server
        loadChats(parsedUser.user_id);
        loadSaved();
        loadCart();
        loadOrders();
        loadCredits();

        // Set timer for remaining session time
        const remaining = SESSION_MS - elapsed;
        const timer = setTimeout(() => clearSession(), remaining);

        setIsReady(true);

        return () => clearTimeout(timer);
      }
    }

    setIsReady(true);
  }, []);

  const handleLogin = (userData) => {
    setUser(userData);
    localStorage.setItem('user', JSON.stringify(userData));
    localStorage.setItem('login_time', Date.now().toString());
    loadChats(userData.user_id);
    loadSaved();
    loadCart();
    loadOrders();
    loadCredits();
  };

  const handleLogout = () => {
    clearSession();
  };

  const selectChat = (chatId) => {
    setCurrentChatId(chatId);
    localStorage.setItem('currentChatId', chatId);
  };

  const handleNewChat = () => {
    setCurrentChatId(null);
    localStorage.removeItem('currentChatId');
  };

  // Single source of truth: update chats dict directly
  const updateChat = (chatId, chatData) => {
    setChats(prev => {
      const updated = { ...prev, [chatId]: chatData };
      if (user) localStorage.setItem(`chats_${user.user_id}`, JSON.stringify(updated));
      return updated;
    });
  };

  const handleNewChatCreated = (chatId, chatData) => {
    setCurrentChatId(chatId);
    localStorage.setItem('currentChatId', chatId);
    setChats(prev => {
      const updated = { ...prev, [chatId]: chatData };
      if (user) localStorage.setItem(`chats_${user.user_id}`, JSON.stringify(updated));
      return updated;
    });
  };

  const deleteChat = async (chatId) => {
    try {
      await api.delete(`/chats/${chatId}`);
    } catch (err) {
      console.error('Failed to delete chat:', err);
      return;
    }
    setChats(prev => {
      const updated = { ...prev };
      delete updated[chatId];
      if (user) localStorage.setItem(`chats_${user.user_id}`, JSON.stringify(updated));
      return updated;
    });
    if (currentChatId === chatId) {
      setCurrentChatId(null);
      localStorage.removeItem('currentChatId');
    }
  };

  const renameChat = async (chatId, title) => {
    const clean = (title || '').trim();
    if (!clean) return;
    try {
      const res = await api.patch(`/chats/${chatId}`, { title: clean });
      updateChat(chatId, res.data.chat);
    } catch (err) {
      console.error('Failed to rename chat:', err);
    }
  };

  // Derive messages from chats — no separate messages state
  const currentMessages = currentChatId
    ? (chats[currentChatId]?.messages || [])
    : [];

  if (!isReady) return null;

  if (!user) {
    if (showAuth) {
      return (
        <Auth
          onLogin={handleLogin}
          initialMode={authMode}
          onBack={() => setShowAuth(false)}
        />
      );
    }
    return (
      <LandingPage
        onGetStarted={() => { setAuthMode('signup'); setShowAuth(true); }}
        onSignIn={() => { setAuthMode('login'); setShowAuth(true); }}
      />
    );
  }

  return (
    <div className="app-container">
      <Sidebar
        isOpen={sidebarOpen}
        chats={chats}
        currentChatId={currentChatId}
        onSelectChat={selectChat}
        onNewChat={handleNewChat}
        onLogout={handleLogout}
        username={user.username}
        searchQuery={searchQuery}
        setSearchQuery={setSearchQuery}
        onDeleteChat={deleteChat}
        onRenameChat={renameChat}
        savedItems={savedItems}
        onUnsave={toggleSave}
        cartItems={cartItems}
        cartTotal={cartTotal}
        orders={orders}
        onRemoveFromCart={removeFromCart}
        onPlaceOrder={placeOrder}
        onCancelOrder={cancelOrder}
      />
      <ChatArea
        sidebarOpen={sidebarOpen}
        onToggleSidebar={() => setSidebarOpen(o => !o)}
        user={user}
        currentChatId={currentChatId}
        chats={chats}
        messages={currentMessages}
        onChatUpdated={updateChat}
        onNewChatCreated={handleNewChatCreated}
        savedPids={savedPids}
        onToggleSave={toggleSave}
        cartPids={cartPids}
        onToggleCart={toggleCart}
        onOrderActivity={refreshOrderState}
        credits={credits}
        onCreditsRefresh={loadCredits}
      />
    </div>
  );
};

export default App;
