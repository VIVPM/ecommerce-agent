import React, { useState } from 'react';
import api from '../api';

const Auth = ({ onLogin }) => {
  const [isLogin, setIsLogin] = useState(true);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      const endpoint = isLogin ? '/auth/login' : '/auth/signup';
      const response = await api.post(endpoint, { username, password });

      onLogin({
        user_id: response.data.user_id,
        username: response.data.username
      });
    } catch (err) {
      setError(err.response?.data?.detail || 'Authentication failed');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="auth-container">
      <div className="auth-card">
        <h1 className="auth-title">Ecommerce Agent</h1>
        <p className="auth-subtitle">
          {isLogin ? 'Welcome back! Please login' : 'Create an account to get started'}
        </p>

        <form onSubmit={handleSubmit}>
          <div className="form-group">
            <label>Username</label>
            <input
              type="text"
              className="form-input"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
            />
          </div>
          <div className="form-group">
            <label>Password</label>
            <input
              type="password"
              className="form-input"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>

          {error && <div style={{ color: 'var(--error-color)', fontSize: '0.85rem', marginBottom: '1rem' }}>{error}</div>}

          <button type="submit" className="btn-primary" disabled={loading}>
            {loading ? 'Processing...' : (isLogin ? 'Login' : 'Signup')}
          </button>
        </form>

        <center>
          <button
            className="btn-link"
            onClick={() => setIsLogin(!isLogin)}
          >
            {isLogin ? 'Don\'t have an account? Signup' : 'Already have an account? Login'}
          </button>
        </center>
      </div>
    </div>
  );
};

export default Auth;
