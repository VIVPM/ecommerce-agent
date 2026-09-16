import React, { useState, useEffect, useRef } from 'react';
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
  const [cartTotal, setCartTotal] = useState(0);
  const [orders, setOrders] = useState([]);
  const [credits, setCredits] = useState(null); // { cap, used, remaining } — daily message allowance
  const [sidebarOpen, setSidebarOpen] = useState(true);

  // The pid SETS are separate state from the item LISTS, deliberately.
  //
  // A chat icon only needs to know "is this in?", and it must answer instantly —
  // it is the thing the user just clicked. The lists carry title, price and price
  // movement, which only the server can produce. Deriving the sets from the lists
  // (`new Set(savedItems.map(...))`) coupled the icon to a full refetch, so every
  // tap waited on a mutation AND a reload before anything moved: the lag, and the
  // flicker when the reload landed. Now the set flips at once and the list
  // reconciles behind it.
  const [savedPids, setSavedPids] = useState(new Set());
  const [cartPids, setCartPids] = useState(new Set());

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
      // Non-critical badge — leave it hidden if the call fails.
    }
  };

  // Two refs, because React state cannot answer "what did the user just ask
  // for?" synchronously.
  //
  // `intent` is the desired membership per pid, written the instant a click
  // happens. Reading `pids` from the render closure instead was wrong: six taps
  // inside ~180ms all saw the SAME snapshot, so instead of alternating they all
  // computed the same wasIn and fired the same request. The UI ended up saying
  // "in cart" while the server had deleted it.
  //
  // `pending` chains the requests per pid so a POST and a DELETE for one product
  // can never be in flight together and land out of order.
  const intent = useRef(new Map());
  const pending = useRef(new Map());

  // Flip the icon NOW, reconcile after. The request still has to happen, but the
  // user should not watch two Neon round trips before the heart fills in — that
  // was ~1.8s of nothing, and the reload landing afterwards is what made it look
  // like the icon came back.
  const toggleMembership = (pid, pids, setPids, reload, path, label) => {
    const current = intent.current.has(pid) ? intent.current.get(pid) : pids.has(pid);
    const desired = !current;
    intent.current.set(pid, desired);        // synchronous: the next click sees it

    setPids(prev => {                        // functional, so it cannot use a stale set
      const next = new Set(prev);
      if (desired) next.add(pid); else next.delete(pid);
      return next;
    });

    // Read the intent at RUN time, not at click time. Taps queued behind each
    // other therefore all send the FINAL state rather than replaying a sequence,
    // which is safe because add is idempotent server-side and a delete-404 just
    // means it is already gone.
    const run = async () => {
      const want = intent.current.get(pid);
      try {
        if (want) await api.post(path, { pid });
        else await api.delete(`${path}/${pid}`);
        reload();                            // NOT awaited: the icon is already right
      } catch (err) {
        if (!want && err?.response?.status === 404) { reload(); return; }
        // A real failure: put the UI back to what the server actually has.
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
        intent.current.delete(pid);          // settled; fall back to server truth
      }
    });
  };

  const toggleSave = (pid) =>
    toggleMembership(pid, savedPids, setSavedPids, loadSaved, '/saved', 'saved product');

  const toggleCart = (pid) =>
    toggleMembership(pid, cartPids, setCartPids, loadCart, '/cart', 'cart');

  const placeOrder = async () => {
    await api.post('/orders');   // errors bubble to the caller, which shows them
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
        onRemoveFromCart={toggleCart}
        orders={orders}
        onPlaceOrder={placeOrder}
        onCancelOrder={cancelOrder}
        onToggleOpen={() => setSidebarOpen(o => !o)}
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
