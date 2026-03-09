import streamlit as st
import os
from faq import ingest_faq_data, faq_chain
from sql import sql_chain
from pathlib import Path
from router import get_router
from dotenv import load_dotenv

from app.db.database import engine, Base
from app.db.models import User, ChatSession
from app.services.chat_manager import load_user_chats, create_new_chat, persist_chat
from app.ui.auth_ui import handle_auth_dialogs, render_auth_buttons
from app.ui.chat_ui import render_chat_sidebar

load_dotenv()
Base.metadata.create_all(bind=engine)

# removed static csv load

def ask(query):
    router = get_router()
    route = router(query).name
    if route == 'faq':
        return faq_chain(query)
    elif route == 'sql':
        return sql_chain(query)
    else:
        return f"Route {route} not implemented yet"

def init_session_state():
    for key in ["logged_in", "show_signup", "is_processing_docs"]:
        st.session_state.setdefault(key, False)
    st.session_state.setdefault("show_login", True)
    st.session_state.setdefault("selected_chat_id", None)
    st.session_state.setdefault("chats", {})
    st.session_state.setdefault("messages", [])

def main():
    st.title("🛒 E-commerce Bot")
    st.caption("Powered by Llama 3.3, Groq, ChromaDB, SQL, and Neon")
    init_session_state()

    # If not logged in, show auth screens
    if not st.session_state.get("logged_in", False):
        handle_auth_dialogs()
        return

    # User just logged in, but chats haven't been loaded from DB
    if st.session_state.logged_in and not st.session_state.chats:
        load_user_chats(st.session_state.user_id)
        if not st.session_state.chats:
            create_new_chat(st)
        else:
            # Select the most recently updated chat
            recent = max(st.session_state.chats.values(), key=lambda x: x["updated_at"])
            st.session_state.selected_chat_id = recent["id"]
            st.session_state.messages = recent["messages"]

    # Sidebar: Chats + Auth Buttons
    render_chat_sidebar()
    render_auth_buttons()

    # Dynamic CSV Uploader in Sidebar
    st.sidebar.markdown("---")
    st.sidebar.subheader("📄 Knowledge Base")
    uploaded_csv = st.sidebar.file_uploader("Upload FAQ CSV to initialize AI", type=["csv"])
    if uploaded_csv and st.sidebar.button("Process & Load Models"):
        with st.spinner("Processing CSV & Loading Models... ⏳"):
            ingest_faq_data(uploaded_csv)
            st.sidebar.success("System initialized with new Knowledge Base!")

    # Main Chat Interface
    chat_id = st.session_state.selected_chat_id
    current_chat = st.session_state.chats.get(chat_id, {"messages": []})
    st.session_state.messages = current_chat["messages"]

    for message in st.session_state.messages:
        with st.chat_message(message['role']):
            st.markdown(message['content'])

    query = st.chat_input("Write your query")
    if query:
        with st.chat_message("user"):
            st.markdown(query)
        st.session_state.messages.append({"role": "user", "content": query})

        # Name chat if it's new
        if current_chat.get("title") == "New Chat":
            new_title = query[:25] + ("..." if len(query) > 25 else "")
            current_chat["title"] = new_title
            
        with st.spinner("Analyzing your query..."):
            response = ask(query)
            
        with st.chat_message("assistant"):
            st.markdown(response)
        st.session_state.messages.append({"role": "assistant", "content": response})

        # Persist to Neon DB
        persist_chat(chat_id)
        
        # Trigger UI refresh to update Sidebar Title if it was changed
        st.rerun()

if __name__ == "__main__":
    main()
