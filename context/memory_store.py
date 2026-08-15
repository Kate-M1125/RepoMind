DB_PATH = "./repo_knowledge_base"

def get_memory_collection():
    # ChromaDB 和嵌入模型属于可选的重型 RAG 依赖，仅在记忆节点真正运行时加载。
    import chromadb
    from chromadb.utils import embedding_functions

    client = chromadb.PersistentClient(path=DB_PATH)
    ef = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name="all-MiniLM-L6-v2"
    )
    return client.get_or_create_collection("issue_memory", embedding_function=ef)
