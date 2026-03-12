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
    if (storedUser) {
      const parsedUser = JSON.parse(storedUser);
      setUser(parsedUser);
      loadChats(parsedUser.user_id);
    }
    const storedKey = localStorage.getItem('gemini_api_key');
    if (storedKey) {
      setGeminiApiKey(storedKey);
    }
    setIsReady(true);
  }, []);

  const handleApiKeyChange = (key) => {
    setGeminiApiKey(key);
    localStorage.setItem('gemini_api_key', key);
  };

  const loadChats = async (userId) => {
    try {
      const response = await api.get(`/chats/${userId}`);
      setChats(response.data.chats || {});
    } catch (err) {
      console.error('Failed to load chats:', err);
    }
  };

  const handleLogin = (userData) => {
    setUser(userData);
    localStorage.setItem('user', JSON.stringify(userData));
    loadChats(userData.user_id);
  };

  const handleLogout = () => {
    setUser(null);
    setChats({});
    setCurrentChatId(null);
    localStorage.removeItem('user');
  };

  const selectChat = (chatId) => {
    setCurrentChatId(chatId);
  };

  const handleNewChat = () => {
    setCurrentChatId(null);
  };

  // Single source of truth: update chats dict directly
  const updateChat = (chatId, chatData) => {
    setChats(prev => ({
      ...prev,
      [chatId]: chatData
    }));
  };

  const handleNewChatCreated = (chatId, chatData) => {
    setCurrentChatId(chatId);
    setChats(prev => ({
      ...prev,
      [chatId]: chatData
    }));
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
