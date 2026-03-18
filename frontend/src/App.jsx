import React, { useState, useEffect } from 'react';
import './index.css';
import Auth from './components/Auth';
import Sidebar from './components/Sidebar';
import ChatArea from './components/ChatArea';
import api from './api';

const App = () => {
  const [user, setUser] = useState(null);
  const [chats, setChats] = useState({});
  const [currentChatId, setCurrentChatId] = useState(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [geminiApiKey, setGeminiApiKey] = useState('');
  const [isReady, setIsReady] = useState(false);

  // Persistence check on mount
  useEffect(() => {
    const storedUser = localStorage.getItem('user');
    const loginTime = localStorage.getItem('login_time');

    if (storedUser && loginTime) {
      const elapsed = Date.now() - parseInt(loginTime);
      const ONE_HOUR = 60 * 60 * 1000;

      if (elapsed > ONE_HOUR) {
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

        // Set timer for remaining session time
        const remaining = ONE_HOUR - elapsed;
        const timer = setTimeout(() => clearSession(), remaining);
        return () => clearTimeout(timer);
      }
    }

    const storedKey = localStorage.getItem('gemini_api_key');
    if (storedKey) setGeminiApiKey(storedKey);
    setIsReady(true);
  }, []);

  const handleApiKeyChange = (key) => {
    setGeminiApiKey(key);
    localStorage.setItem('gemini_api_key', key);
  };

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

  const clearSession = () => {
    setUser(null);
    setChats({});
    setCurrentChatId(null);
    setGeminiApiKey('');
    localStorage.removeItem('token');
    localStorage.removeItem('user');
    localStorage.removeItem('login_time');
    localStorage.removeItem('gemini_api_key');
    setIsReady(true);
  };

  const handleLogin = (userData) => {
    setUser(userData);
    localStorage.setItem('user', JSON.stringify(userData));
    localStorage.setItem('login_time', Date.now().toString());
    loadChats(userData.user_id);
  };

  const handleLogout = () => {
    clearSession();
  };

  const selectChat = (chatId) => {
    setCurrentChatId(chatId);
  };

  const handleNewChat = () => {
    setCurrentChatId(null);
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
    setChats(prev => {
      const updated = { ...prev, [chatId]: chatData };
      if (user) localStorage.setItem(`chats_${user.user_id}`, JSON.stringify(updated));
      return updated;
    });
  };

  // Derive messages from chats — no separate messages state
  const currentMessages = currentChatId
    ? (chats[currentChatId]?.messages || [])
    : [];

  if (!isReady) return null;

  if (!user) {
    return <Auth onLogin={handleLogin} />;
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
        geminiApiKey={geminiApiKey}
        onApiKeyChange={handleApiKeyChange}
      />
      <ChatArea
        user={user}
        currentChatId={currentChatId}
        messages={currentMessages}
        onChatUpdated={updateChat}
        onNewChatCreated={handleNewChatCreated}
      />
    </div>
  );
};

export default App;
