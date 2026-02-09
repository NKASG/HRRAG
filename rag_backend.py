import os
import uuid
import logging
from pathlib import Path
from typing import Dict, Any

import chromadb
from sentence_transformers import SentenceTransformer
from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_groq import ChatGroq
from dotenv import load_dotenv

load_dotenv()

# ======================================================
# LOGGING SETUP
# ======================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("OrangeHR")


# ======================================================
# FULL HR KEYWORD ONTOLOGY (VERY LARGE)
# ======================================================
HR_KEYWORDS = set([
    # Leave
    "leave", "casual leave", "sick leave", "annual leave", "vacation", "holiday",
    "maternity", "paternity", "compassionate leave", "absence",
    # Salary / Payroll
    "salary", "pay", "wage", "payment", "payroll", "allowance", "bonus",
    "deduction", "stipend", "compensation", "earnings",
    # HR Policies
    "policy", "handbook", "guideline", "code of conduct", "rules",
    # Hiring / Onboarding
    "onboarding", "hiring", "recruitment", "job offer", "probation",
    "orientation", "induction", "employment",
    # Departments / Relations
    "hr", "human resource", "employee", "staff", "relations", "team",
    "supervisor", "manager", "performance",
    # Performance
    "review", "kpi", "evaluation", "appraisal", "promotion", "rating",
    # Benefits
    "benefit", "insurance", "medical", "health", "retirement", "pension",
    "gratuity", "allowance",
    # Conduct
    "discipline", "warning", "harassment", "complaint", "grievance",
    "termination", "exit", "resignation", "dismissal",
    # Work Hours
    "work hours", "attendance", "shift", "overtime", "lateness",
    # Training
    "training", "development", "learning", "workshop", "course",
    # Loans
    "loan", "salary advance",
    # Safety
    "safety", "workplace safety", "security",
])


# ======================================================
# MASTER PIPELINE
# ======================================================
class MasterHRPipeline:

    def __init__(self, data_dir="./data", groq_api_key=None):
        self.data_dir = data_dir
        self.groq_api_key = groq_api_key or os.getenv("GROQ_API_KEY")
        self.history = []

        logger.info("Loading embedding model...")
        self.embed_model = SentenceTransformer("all-MiniLM-L6-v2")

        logger.info("Initializing vector store...")
        self.chroma_client = chromadb.PersistentClient(path="./vector_store")
        self.collection = self.chroma_client.get_or_create_collection("hr_documents")

        logger.info("Loading LLM...")
        self.llm = ChatGroq(
            groq_api_key=self.groq_api_key,
            model_name="llama-3.1-8b-instant",
            temperature=0.0
        )

        if self.collection.count() == 0:
            self.ingest_data()
        else:
            logger.info(f"Vector DB ready with {self.collection.count()} documents.")

    # ======================================================
    # INGESTION
    # ======================================================
    def ingest_data(self):
        logger.info("Scanning PDFs...")
        pdf_files = list(Path(self.data_dir).glob("**/*.pdf"))

        if not pdf_files:
            logger.warning("No PDFs found.")
            return

        documents = []
        for pdf in pdf_files:
            try:
                loader = PyPDFLoader(str(pdf))
                loaded_docs = loader.load()
                for doc in loaded_docs:
                    doc.metadata["source"] = pdf.name
                documents.extend(loaded_docs)
            except Exception as e:
                logger.error(f"Failed to load {pdf}: {e}")

        splitter = RecursiveCharacterTextSplitter(
            chunk_size=500,
            chunk_overlap=150
        )
        chunks = splitter.split_documents(documents)

        texts = [c.page_content for c in chunks]
        metadatas = [c.metadata for c in chunks]
        ids = [str(uuid.uuid4()) for _ in range(len(chunks))]

        logger.info(f"Embedding {len(texts)} chunks...")
        vectors = self.embed_model.encode(texts).tolist()

        self.collection.add(
            documents=texts,
            embeddings=vectors,
            metadatas=metadatas,
            ids=ids
        )

        logger.info("Ingestion completed.")

    # ======================================================
    # QUERY PIPELINE
    # ======================================================
    def query(self, question: str, top_k=3) -> Dict[str, Any]:
        q_lower = question.lower()

        # ---------------------------------------------
        # 1. Embed & Retrieve
        # ---------------------------------------------
        query_vec = self.embed_model.encode([question]).tolist()
        results = self.collection.query(
            query_embeddings=query_vec,
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )

        # ---------------------------------------------
        # 2. Filter relevant documents
        # ---------------------------------------------
        valid_indices = [
            i for i, dist in enumerate(results["distances"][0])
            if dist < 1.5
        ]

        # ---------------------------------------------
        # 3. If NO DOCUMENT MATCHES
        # ---------------------------------------------
        if not valid_indices:
            return self._handle_no_match(question)

        # ---------------------------------------------
        # 4. Build context
        # ---------------------------------------------
        context = "\n\n".join(
            results["documents"][0][i] for i in valid_indices
        )

        sources = [
            {
                "source": results["metadatas"][0][i].get("source", "Unknown"),
                "page": results["metadatas"][0][i].get("page", "N/A"),
            }
            for i in valid_indices
        ]

        # ---------------------------------------------
        # 5. LLM Answer
        # ---------------------------------------------
        prompt = f"""
Using ONLY the HR policy text below, answer the user's question clearly.

CONTEXT:
{context}

QUESTION: {question}

If the context does NOT contain the answer, reply with:
"Please kindly visit Louisa at the HR office for proper assistance."
"""

        try:
            llm_response = self.llm.invoke(prompt).content.strip()
        except Exception as e:
            logger.error(e)
            llm_response = "HR system unavailable. Please try again."

        # ---------------------------------------------
        # 6. Save History
        # ---------------------------------------------
        self.history.append({
            "question": question,
            "answer": llm_response,
            "sources": sources,
        })

        return {
            "answer": llm_response,
            "sources": sources,
            "history": self.history
        }

    # ======================================================
    # HANDLING NO MATCH FOUND
    # ======================================================
    def _handle_no_match(self, question: str):
        q = question.lower()

        # HR related but unavailable → ALWAYS Louisa
        if any(keyword in q for keyword in HR_KEYWORDS):
            reply = "Please kindly visit Louisa at the HR office for proper assistance."
        else:
            # Non‑HR question → polite refusal
            prompt = f"""
You are Orange-HR, an HR-only assistant.
The user asked something outside HR:

"{question}"

Reply in 1–2 sentences:
- Politely refuse.
- Tell them you ONLY handle HR matters.
- Redirect them back to HR topics.
- Mention Louisa ONLY for HR-related issues.
"""
            try:
                reply = self.llm.invoke(prompt).content.strip()
            except:
                reply = "I can only assist with HR-related questions."

        self.history.append({
            "question": question,
            "answer": reply,
            "sources": []
        })

        return {"answer": reply, "sources": [], "history": self.history}

# ==========================================
# USAGE EXAMPLE
# ==========================================

# 1. Initialize (This will ingest data if the folder 'data' has PDFs and DB is empty)
hr_bot = MasterHRPipeline(data_dir="./data")

# # 2. Ask a question (This mimics the Advanced Pipeline)
# print("\n--- Test 1: Valid Question ---")
# response_fallback = hr_bot.query(
#     "Can you calculate my leave allowance as a junior manager?"
# )
# print(response_fallback['answer'])


# # 3. Ask a question that likely doesn't exist (Tests the Louisa Fallback)
# print("\n--- Test 2: Invalid/Unknown Question ---")
# response_fallback = hr_bot.query("If my salary is 100,000 what will my leave allowance be?")
# print(response_fallback['answer'])

# # 3. Ask a question that likely doesn't exist (Tests the Louisa Fallback)
# print("\n--- Test 3: Invalid/Unknown Question ---")
# response_fallback = hr_bot.query("How do i get promoted?")
# print(response_fallback['answer'])