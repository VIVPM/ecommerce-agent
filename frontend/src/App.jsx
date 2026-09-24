import React, { useState, useEffect, useRef } from 'react';
import './index.css';
import Auth from './components/Auth';
import LandingPage from './components/LandingPage';
import Sidebar from './components/Sidebar';
import ChatArea from './components/ChatArea';
import api from './api';

// Phone-width check, read at the moment it matters rather than stored, so a
// rotated or resized window is judged by its current width.
const isNarrow = () => window.matchMedia('(max-width: 768px)').matches;

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
  const [cartTotal, setCartTotal] = useState(0);
  const [orders, setOrders] = useState([]);
  const [credits, setCredits] = useState(null);
  const [sidebarOpen, setSidebarOpen] = useState(() => !isNarrow());
  const [preferences, setPreferences] = useState('');

  const [savedPids, setSavedPids] = useState(new Set());
  const [cartPids, setCartPids] = useState(new Set());

  const loadChats = async (userId) => {
    try {
      const response = await api.get('/chats');
      const freshChats = response.data.chats || {};
      setChats(freshChats);
      localStorage.setItem(`chats_${userId}`, JSON.stringify(freshChats));
    } catch (err) {
      console.error('Failed to load chats (server may be waking up):', err);
    }
  };

  const loadSaved = async () => {
    try {
      const res = await api.get('/saved');
      const items = res.data.saved || [];
      setSavedItems(items);
      setSavedPids(new Set(items.map(s => s.pid)));
    } catch (err) {
      console.error('Failed to load saved products:', err);
    }
  };

  const loadCart = async () => {
    try {
      const res = await api.get('/cart');
      const items = res.data.cart || [];
      setCartItems(items);
      setCartTotal(res.data.total || 0);
      setCartPids(new Set(items.map(c => c.pid)));
    } catch (err) {
      console.error('Failed to load cart:', err);
    }
  };

  const loadPreferences = async () => {
    try {
      const res = await api.get('/preferences');
      setPreferences(res.data.preferences || '');
    } catch (err) {
      console.error('Failed to load preferences:', err);
    }
  };

  const savePreferences = async (text) => {
    setPreferences(text);
    try {
      await api.put('/preferences', { text });
    } catch (err) {
      console.error('Failed to save preferences:', err);
      loadPreferences();
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

  const loadCredits = async () => {
    try {
      const res = await api.get('/account/credits');
      setCredits(res.data);
    } catch {
    }
  };

  const intent = useRef(new Map());
  const pending = useRef(new Map());

  const toggleMembership = (pid, pids, setPids, reload, path, label) => {
    const current = intent.current.has(pid) ? intent.current.get(pid) : pids.has(pid);
    const desired = !current;
    intent.current.set(pid, desired);

    setPids(prev => {
      const next = new Set(prev);
      if (desired) next.add(pid); else next.delete(pid);
      return next;
    });

    const run = async () => {
      const want = intent.current.get(pid);
      try {
        if (want) await api.post(path, { pid });
        else await api.delete(`${path}/${pid}`);
        reload();
      } catch (err) {
        if (!want && err?.response?.status === 404) { reload(); return; }
        intent.current.delete(pid);
        setPids(prev => {
          const next = new Set(prev);
          if (want) next.delete(pid); else next.add(pid);
          return next;
        });
        console.error(`Failed to update ${label}:`, err);
      }
    };

    const queued = (pending.current.get(pid) || Promise.resolve()).then(run, run);
    pending.current.set(pid, queued);
    queued.finally(() => {
      if (pending.current.get(pid) === queued) {
        pending.current.delete(pid);
        intent.current.delete(pid);
      }
    });
  };

  const toggleSave = (pid) =>
    toggleMembership(pid, savedPids, setSavedPids, loadSaved, '/saved', 'saved product');

  const toggleCart = (pid) =>
    toggleMembership(pid, cartPids, setCartPids, loadCart, '/cart', 'cart');

  const placeOrder = async () => {
    await api.post('/orders');
    await Promise.all([loadCart(), loadOrders()]);
  };

  const cancelOrder = async (orderId) => {
    await api.post(`/orders/${orderId}/cancel`);
    await loadOrders();
  };

  const clearSession = () => {
    setUser(null);
    setChats({});
    setCurrentChatId(null);
    setSavedItems([]);
    setCredits(null);
    setShowAuth(false);
    localStorage.removeItem('token');
    localStorage.removeItem('user');
    localStorage.removeItem('login_time');
    localStorage.removeItem('currentChatId');
    setIsReady(true);
  };

  useEffect(() => {
    const storedUser = localStorage.getItem('user');
    const loginTime = localStorage.getItem('login_time');

    if (storedUser && loginTime) {
      const elapsed = Date.now() - parseInt(loginTime);
      const SESSION_MS = 12 * 60 * 60 * 1000;

      if (elapsed > SESSION_MS) {
        clearSession();
      } else {
        const parsedUser = JSON.parse(storedUser);
        setUser(parsedUser);

        const cachedChats = localStorage.getItem(`chats_${parsedUser.user_id}`);
        if (cachedChats) setChats(JSON.parse(cachedChats));

        loadChats(parsedUser.user_id);
        loadSaved();
        loadCart();
        loadOrders();
        loadPreferences();
        loadCredits();

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
    loadPreferences();
    loadCredits();
  };

  const handleLogout = () => {
    clearSession();
  };

  const selectChat = (chatId) => {
    setCurrentChatId(chatId);
    localStorage.setItem('currentChatId', chatId);
    if (isNarrow()) setSidebarOpen(false);
  };

  const handleNewChat = () => {
    setCurrentChatId(null);
    localStorage.removeItem('currentChatId');
    if (isNarrow()) setSidebarOpen(false);
  };

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
      {sidebarOpen && (
        <div className="sidebar-backdrop" onClick={() => setSidebarOpen(false)} />
      )}
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
        onRemoveFromCart={toggleCart}
        orders={orders}
        onPlaceOrder={placeOrder}
        onCancelOrder={cancelOrder}
        onToggleOpen={() => setSidebarOpen(o => !o)}
        preferences={preferences}
        onSavePreferences={savePreferences}
      />
      <ChatArea
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
        credits={credits}
        onCreditsRefresh={loadCredits}
      />
    </div>
  );
};

export default App;
