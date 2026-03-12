import axios from 'axios';

const api = axios.create({
  baseURL: 'http://localhost:8000/api',
});

// Add a request interceptor to include the API key header if available
api.interceptors.request.use((config) => {
  const geminiKey = localStorage.getItem('gemini_api_key');
  if (geminiKey) {
    config.headers['X-Gemini-API-Key'] = geminiKey;
  }
  return config;
}, (error) => {
  return Promise.reject(error);
});

export default api;
