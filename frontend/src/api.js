// Configures the authenticated HTTP client used by the frontend.
import axios from "axios";

const api = axios.create({


  baseURL: import.meta.env.VITE_API_BASE_URL || "https://ecommerce-agent-29hh.onrender.com/api",
});


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
