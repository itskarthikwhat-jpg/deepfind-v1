import search_engine as appu

appu.init_db("./test_lancedb")

# Fake docs, as if HP/Aaling already pushed them
appu.upsert({
    "file_path": "C:/fake/resume.pdf",
    "file_name": "resume.pdf",
    "file_type": "pdf",
    "content": "Experienced data analyst skilled in Python and SQL",
    "vector": None,
    "modified_time": 1234567890.0,
})

appu.upsert({
    "file_path": "C:/fake/notes.txt",
    "file_name": "notes.txt",
    "file_type": "txt",
    "content": "Grocery list: milk, eggs, bread",
    "vector": None,
    "modified_time": 1234567891.0,
})

# Exact keyword match test
print("--- keyword search: 'Python' ---")
for r in appu.search("Python"):
    print(r)

# Meaning match test (no exact keyword overlap)
print("\n--- meaning search: 'data science skills' ---")
for r in appu.search("data science skills"):
    print(r)

# Delete test
appu.delete("C:/fake/notes.txt")
print("\n--- after deleting notes.txt, search 'grocery' ---")
print(appu.search("grocery"))  # should be empty
