import React, { useState, useEffect } from 'react';
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
  const [credits, setCredits] = useState(null); // { cap, used, remaining } — daily message allowance

  // Set of saved pids, for O(1) lookup when rendering product links in chat
  const savedPids = new Set(savedItems.map(s => s.pid));

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

  // Save/unsave from anywhere; refresh the list so price movement stays current.
  const toggleSave = async (pid) => {
    const isSaved = savedPids.has(pid);
    try {
      if (isSaved) {
        await api.delete(`/saved/${pid}`);
      } else {
        await api.post('/saved', { pid });
      }
      await loadSaved();
    } catch (err) {
      console.error('Failed to update saved product:', err);
    }
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
        credits={credits}
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
        credits={credits}
        onCreditsRefresh={loadCredits}
      />
    </div>
  );
};

export default App;
