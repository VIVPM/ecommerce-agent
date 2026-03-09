import streamlit as st
from app.services.authentication import signup, login

def render_auth_buttons():
    if not st.session_state.get("logged_in", False):
        st.sidebar.markdown("---")
        if st.sidebar.button("Login"):
            st.session_state.show_login = True
            st.session_state.show_signup = False
            st.rerun()
        if st.sidebar.button("Submit as new user"):
            st.session_state.show_signup = True
            st.session_state.show_login = False
            st.rerun()
    else:
        st.sidebar.markdown("---")
        st.sidebar.write(f"Logged in as **{st.session_state.username}**")
        if st.sidebar.button("Logout"):
            for key in ["logged_in", "user_id", "username", "chats", "selected_chat_id", "messages"]:
                if key in st.session_state:
                    del st.session_state[key]
            st.rerun()

def handle_auth_dialogs():
    if not st.session_state.get("logged_in", False):
        if st.session_state.get("show_signup", False):
            st.subheader("Signup")
            with st.form("signup_form"):
                u = st.text_input("Username")
                p = st.text_input("Password", type="password")
                submit = st.form_submit_button("Signup")
            if submit:
                err = signup(u, p, st)
                if err: st.error(err)
                else: st.rerun()
            if st.button("Back to Login"):
                st.session_state.show_login, st.session_state.show_signup = True, False
                st.rerun()
            st.stop()

        if st.session_state.get("show_login", True):
            st.subheader("Login")
            with st.form("login_form"):
                u = st.text_input("Username")
                p = st.text_input("Password", type="password")
                submit = st.form_submit_button("Login")
            if submit:
                err = login(u, p, st)
                if err: st.error(err)
                else: st.rerun()
            if st.button("Create new account"):
                st.session_state.show_login, st.session_state.show_signup = False, True
                st.rerun()
            st.stop()
