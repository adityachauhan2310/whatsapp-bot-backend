# ai_prompts.py

# Groq/AI prompt templates for the WhatsApp Bot project

# Summarization prompt for group conversations
group_summary_prompt = (
    "You are a helpful assistant that summarizes group conversations."
)

# Summarization prompt for text
text_summary_prompt = (
    "You are a helpful assistant that summarizes text."
)

# Keyword extraction and summary prompt
keyword_summary_prompt = (
    "You are a helpful assistant that extracts keywords and generates concise summaries."
)

# Contextual reply system prompt
contextual_reply_system_prompt = (
    "You are a helpful customer service assistant."
)

# Contextual reply user prompt template (use .format(context=context) to fill)
contextual_reply_user_prompt = (
    "Based on the following WhatsApp conversation, generate a helpful and contextual response.\n"
    "The response should be professional, concise, and address any questions or concerns raised.\n\n"
    "Recent messages:\n{context}\n\nGenerate a response:"
) 