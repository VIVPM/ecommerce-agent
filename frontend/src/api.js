import axios from "axios";

const api = axios.create({
  baseURL:
    "https://ecommerce-agent-29hh.onrender.com/api" ||
    "http://localhost:8000/api",
});

// Attach JWT token to every request
api.interceptors.request.use(
  (config) => {
    const token = localStorage.getItem("token");
    if (token) {
      config.headers["Authorization"] = `Bearer ${token}`;
    }
    return config;
  },
  (error) => {
    return Promise.reject(error);
  },
);

// On 401, clear session (token expired or invalid)
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      localStorage.removeItem("token");
      localStorage.removeItem("user");
      localStorage.removeItem("login_time");
      window.location.reload();
    }
    return Promise.reject(error);
  },
);

export default api;
