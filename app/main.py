import streamlit as st
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

# Add the project root to sys.path so 'app.xyz' imports work
project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root))

from app.faq import ingest_faq_data, faq_chain
from app.sql import sql_chain
from app.router import get_semantic_router
from app.memory import optimize_query


from app.db.database import engine, Base
from app.db.models import EcommerceAccount
from app.services.chat_manager import load_user_chats, create_new_chat, persist_chat
from app.ui.auth_ui import handle_auth_dialogs, render_auth_buttons
from app.ui.chat_ui import render_chat_sidebar

Base.metadata.create_all(bind=engine)

# removed static csv load

def ask(query, history):
    optimized_query = optimize_query(query, history)
    if optimized_query != query:
        print(f"Original Query: {query} -> Optimized Query: {optimized_query}")
        
    router = get_semantic_router()
    route_result = router(optimized_query)
    route = route_result.name if route_result else None
    
    # If explicitly sql, go to sql. Otherwise, default everything else to faq
    if route == 'sql':
        return sql_chain(optimized_query)
    else: 
        # routes 'faq' and None (Unsure) to the RAG pipeline
        return faq_chain(optimized_query)

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
            history_subset = st.session_state.messages[-6:-1] # Get up to 5 previous messages, excluding the current one
            response = ask(query, history_subset)
            
        with st.chat_message("assistant"):
            st.markdown(response)
        st.session_state.messages.append({"role": "assistant", "content": response})

        # Persist to Neon DB
        st.session_state.chats[chat_id]["messages"] = st.session_state.messages
        persist_chat(chat_id)
        
        # Trigger UI refresh to update Sidebar Title if it was changed
        st.rerun()

if __name__ == "__main__":
    main()
