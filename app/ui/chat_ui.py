import streamlit as st
from app.services.chat_manager import create_new_chat

def render_chat_sidebar():
    st.sidebar.title("Chat History")
    
    if st.sidebar.button("➕ New Chat", use_container_width=True):
        create_new_chat(st)
        st.rerun()

    st.sidebar.markdown("### Recent Chats")
    
    if "chats" in st.session_state and st.session_state.chats:
        # Sort by updated_at descending
        sorted_chats = sorted(
            st.session_state.chats.values(), 
            key=lambda x: x.get("updated_at", ""), 
            reverse=True
        )
        
        for chat in sorted_chats:
            title = chat.get("title", "New Chat")
            title = title if len(title) <= 25 else title[:22] + "..."
            
            # Highlight selected chat
            is_selected = chat["id"] == st.session_state.get("selected_chat_id")
            btn_type = "primary" if is_selected else "secondary"
            
            if st.sidebar.button(f"💬 {title}", key=f"chat_btn_{chat['id']}", type=btn_type, use_container_width=True):
                st.session_state.selected_chat_id = chat["id"]
                st.session_state.messages = chat["messages"]
                st.rerun()
    else:
        st.sidebar.info("No chat history found.")
