from semantic_router import Route, SemanticRouter
from semantic_router.encoders import HuggingFaceEncoder
import streamlit as st

faq = Route(
    name='faq',
    utterances=[
        "What is the return policy of the products?",
        "Do I get discount with the HDFC credit card?",
        "How can I track my order?",
        "What payment methods are accepted?",
        "How long does it take to process a refund?",
        "What is your policy on defective products?",
        "What do I do if I receive a damaged item?",
        "Is there a warranty on your products?",
        "How do I cancel my order?",
        "What is the delivery time?",
        "Do you offer cash on delivery?",
        "How do I contact customer support?",
    ]
)

sql = Route(
    name='sql',
    utterances=[
        "I want to buy nike shoes that have 50% discount.",
        "Are there any shoes under Rs. 3000?",
        "Do you have formal shoes in size 9?",
        "Are there any Puma shoes on sale?",
        "What is the price of puma running shoes?",
        "Show me the top rated shoes.",
        "Which shoes have the highest discount?",
        "List all Adidas shoes available.",
        "Show me running shoes for women under 2000 rupees.",
        "What are the best shoes with rating above 4.5?",
        "Do you have casual shoes between Rs. 1000 and Rs. 5000?",
        "Show me 5 shoes with the most reviews.",
        "I am looking for sports shoes with at least 20 percent off.",
        "Which brands sell shoes under Rs. 1500?",
        "Show me all shoes sorted by price low to high.",
        "Are there any leather shoes with good ratings?",
        "Find me sneakers with more than 1000 reviews.",
        "What Reebok shoes do you have?",
        "Give me shoes with a discount greater than 30 percent.",
        "Show top 3 shoes in descending order of rating.",
        "I need sandals in size 8.",
        "Find me the cheapest shoes you have.",
        "What are shoes available in the 500 to 1000 rupee range?",
    ]
)

@st.cache_resource(show_spinner="Initializing Semantic Router... ⏳")
def get_router():
    encoder = HuggingFaceEncoder(
        name="sentence-transformers/all-MiniLM-L6-v2"
    )
    return SemanticRouter(routes=[faq, sql], encoder=encoder, auto_sync="local")

if __name__ == "__main__":
    print(router("What is your policy on defective product?").name)
    print(router("Pink Puma shoes in price range 5000 to 1000").name)