import os
import json
import logging
import asyncio
from typing import List, Optional, Dict
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from litellm import acompletion
from tenacity import retry, stop_after_attempt, wait_exponential
from litellm import CustomStreamWrapper
from litellm.files.main import ModelResponse

logger = logging.getLogger(__name__)
load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

############################################
# Pydantic models for input text data structures
############################################

class TextPage(BaseModel):
    """Represents a single page from a PDF document."""
    page_number: int = Field(..., description="1-based page number")
    text_content: str = Field(..., description="Extracted text content from the page")

class TextDocument(BaseModel):
    """Represents a document with multiple pages."""
    doc_id: str = Field(..., description="Unique document identifier")
    pages: List[TextPage] = Field(default_factory=list, description="List of pages in the document")

class TextPublication(BaseModel):
    """Represents a publication containing multiple documents."""
    pub_id: str = Field(..., description="Unique publication identifier")
    documents: List[TextDocument] = Field(default_factory=list, description="List of documents in the publication")

############################################
# Pydantic models for output text data structures
############################################

class SectionPageRange(BaseModel):
    """Represents a section in the text."""
    start_page: int = Field(..., description="Start page of the section")
    end_page: int = Field(..., description="End page of the section")

class Sections(BaseModel):
    """Represents the JSON structure expected from the LLM."""
    front_matter: SectionPageRange | None = Field(description="Front matter section")
    contents: SectionPageRange | None = Field(description="Contents section")
    list_of_figures: SectionPageRange | None = Field(description="List of figures section")
    list_of_tables: SectionPageRange | None = Field(description="List of tables section")
    body: SectionPageRange | None = Field(description="Body section")
    references: SectionPageRange | None = Field(description="References section")
    end_notes: SectionPageRange | None = Field(description="End notes section")
    annexes: SectionPageRange | None = Field(description="Annexes or appendices sections")

############################################
# LLM call with retry
############################################
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
async def process_text(text_document: TextDocument) -> Optional[Dict]:
    """Process text content using the LLM with retry logic."""
    try:
        prompt = f"""
Take the following document text and identify the page ranges for each major section of the document, if present.
Return JSON with the following fields:
- front_matter: page range for title page, abstract, etc. (optional)
- contents: page range for table of contents if present (optional)
- list_of_figures: page range for list of figures if present (optional)
- list_of_tables: page range for list of tables if present (optional)
- body: page range for main content (required)
- references: page range for references/bibliography if present (optional)
- end_notes: page range for endnotes if present (optional)
- annexes: page ranges for annexes or appendices if present (optional)

Each section should have a start_page and end_page number.

Text content:
```text
{str(text_document.pages)}
```
"""
        logger.debug("Sending request to LLM")
        response: ModelResponse | CustomStreamWrapper = await acompletion(
            model="gemini/gemini-2.0-flash-001", 
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object", "response_schema": Sections.model_json_schema()},
            api_key=os.getenv('GEMINI_API_KEY')
        )
        logger.debug("Received response from LLM")
        if isinstance(response, CustomStreamWrapper):
            raise ValueError("Streaming response not supported")
        else:
            content = response['choices'][0]['message']['content']
        result = {text_document.doc_id: Sections.model_validate_json(content)}
        return result
    except Exception as e:
        logger.error(f"Error during text processing: {str(e)}")
        return None

############################################
# Main workflow function
############################################
async def process_documents_concurrently(input_file: str, output_file: str, max_concurrency: int = 5):
    """
    Process multiple documents concurrently and save the headings to a JSON file.
    Skip documents that already have entries in the output file.
    
    Args:
        input_file: Path to the input JSON file containing text content
        output_file: Path to save the output headings JSON
        max_concurrency: Maximum number of concurrent LLM requests
    """
    logger.info(f"Loading text content from {input_file}")
    
    try:
        # Load existing headings data if output file exists
        existing_headings = {}
        if os.path.exists(output_file):
            logger.info(f"Loading existing headings from {output_file}")
            with open(output_file, 'r') as f:
                existing_headings = json.load(f)
            logger.info(f"Found {len(existing_headings)} existing document entries")
        
        with open(input_file, 'r') as f:
            content = json.load(f)
        
        # The content is an array of publications
        publications = []
        for pub_data in content:
            try:
                publication = TextPublication.model_validate(pub_data)
                publications.append(publication)
            except Exception as e:
                logger.error(f"Error parsing publication: {str(e)}")
        
        logger.info(f"Found {len(publications)} publications")
        
        # Create a semaphore to limit concurrency
        semaphore = asyncio.Semaphore(max_concurrency)
        
        # Collect all documents from all publications that don't have existing entries
        all_documents = []
        for pub in publications:
            for doc in pub.documents:
                if doc.doc_id not in existing_headings:
                    all_documents.append(doc)
            
        logger.info(f"Found {len(all_documents)} new documents to process")
        
        async def process_with_semaphore(document):
            async with semaphore:
                logger.info(f"Processing document: {document.doc_id}")
                return await process_text(document)
        
        # Create tasks for all new documents
        tasks = [process_with_semaphore(doc) for doc in all_documents]
        
        # Execute all tasks concurrently and gather results
        results = await asyncio.gather(*tasks)
        
        # Filter out None results (failed processing)
        results = [r for r in results if r is not None]
        
        # Combine all results with existing headings
        headings_data = existing_headings.copy()  # Start with existing data
        for result in results:
            # Each result is a dictionary with doc_id as key and Sections object as value
            for doc_id, sections_obj in result.items():
                # Convert Sections object to a dictionary
                headings_data[doc_id] = sections_obj.model_dump()
        
        # Save results to output file
        logger.info(f"Saving headings to {output_file}")
        with open(output_file, 'w') as f:
            json.dump(headings_data, f, indent=2)
            
        logger.info(f"Successfully processed {len(results)} new documents. Total documents: {len(headings_data)}")
        
        # Check for any documents that failed processing
        failed_docs = [doc.doc_id for doc in all_documents if doc.doc_id not in headings_data]
        if failed_docs:
            logger.error(f"Failed to process {len(failed_docs)} documents: {failed_docs}")
        
    except Exception as e:
        logger.error(f"Error processing documents: {str(e)}")
        raise

if __name__ == "__main__":
    # Define input and output file paths
    input_file = "transform/text/text_content.json"
    output_file = "transform/text/sections.json"
    
    # Run the async function in the event loop
    asyncio.run(process_documents_concurrently(input_file, output_file))
