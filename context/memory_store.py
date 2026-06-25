import chromadb
from chromadb.utils import embedding_functions

DB_PATH = "./repo_knowledge_base"

def get_memory_collection():
    client = chromadb.PersistentClient(path=DB_PATH)
    ef = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name="all-MiniLM-L6-v2"
    )
    return client.get_or_create_collection("issue_memory", embedding_function=ef)