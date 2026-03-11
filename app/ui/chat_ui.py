import streamlit as st
from app.services.chat_manager import create_new_chat

def render_chat_sidebar():
    st.sidebar.title("Chat History")
    
    if st.sidebar.button("➕ New Chat", use_container_width=True):
        create_new_chat(st)
        st.session_state.chat_displayed_count = 10
        st.rerun()

    c1, c2 = st.sidebar.columns([0.85, 0.15])
    with c1:
        q = st.text_input(
            "Search chats",
            value=st.session_state.chat_search_query,
            label_visibility="collapsed",
            placeholder="Search conversations…",
            key="chat_search_input"
        )
        if q != st.session_state.chat_search_query:
            st.session_state.chat_search_query = q
            st.session_state.chat_displayed_count = 10  # reset page
            st.rerun()
    with c2:
        if st.button("✕", key="chat_search_clear", help="Clear search"):
            st.session_state.chat_search_query = ""
            st.session_state.chat_displayed_count = 10
            st.rerun()

    st.sidebar.markdown("### Recent Chats")
    
    if "chats" in st.session_state and st.session_state.chats:
        # Sort by updated_at descending, filter out chats with no messages
        sorted_chats = sorted(
            [c for c in st.session_state.chats.values() if c.get("messages")], 
            key=lambda x: x.get("updated_at", ""), 
            reverse=True
        )
        
        q = st.session_state.chat_search_query.strip().lower()
        if q:
            sorted_chats = [
                c for c in sorted_chats
                if any(q in (m.get("content", "").lower()) for m in c.get("messages", []))
                   or q in (c.get("title") or "").lower()
            ]

        # Slice for current page
        start = 0
        end = st.session_state.chat_displayed_count
        displayed = sorted_chats[start:end]

        for chat in displayed:
            title = chat.get("title", "New Chat")
            title = title if len(title) <= 25 else title[:22] + "..."
            
            # Highlight selected chat
            is_selected = chat["id"] == st.session_state.get("selected_chat_id")
            btn_type = "primary" if is_selected else "secondary"
            
            if st.sidebar.button(f"💬 {title}", key=f"chat_btn_{chat['id']}", type=btn_type, use_container_width=True):
                st.session_state.selected_chat_id = chat["id"]
                st.session_state.messages = chat["messages"]
                st.rerun()
                
        # Pager: show more button if more remain
        remaining = max(0, len(sorted_chats) - end)
        if remaining > 0:
            show_n = min(remaining, 10)
            if st.sidebar.button(f"Show more ({show_n})", key="show_more_chats", use_container_width=True):
                st.session_state.chat_displayed_count += 10
                st.rerun()
        else:
            if sorted_chats and len(sorted_chats) > 10:
                st.sidebar.caption("No more chats.")
                
    else:
        st.sidebar.info("No chat history found.")
