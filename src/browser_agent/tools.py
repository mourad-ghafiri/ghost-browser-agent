"""Tool definitions for LLM function calling (OpenAI-compatible)."""

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click on an element by its ID number from the element list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "Element ID from the page"},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Clear an input field and type new text into it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "Element ID of the input field"},
                    "text": {"type": "string", "description": "Text to type into the field"},
                },
                "required": ["id", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "select_option",
            "description": "Select an option from a dropdown/select element.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "Element ID of the select element"},
                    "value": {"type": "string", "description": "Value of the option to select"},
                },
                "required": ["id", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll",
            "description": "Scroll the page up or down.",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down"],
                        "description": "Scroll direction",
                    },
                },
                "required": ["direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "navigate",
            "description": "Navigate to a specific URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL to navigate to"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "go_back",
            "description": "Go back to the previous page in browser history.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": "Wait for a specified number of seconds (1-5) for page content to load.",
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "number",
                        "description": "Seconds to wait (1-5)",
                    },
                },
                "required": ["seconds"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_text",
            "description": "Get the full text content of a specific element.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "Element ID to extract text from"},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hover",
            "description": "Hover over an element to reveal tooltips or dropdown menus.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "Element ID to hover over"},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Signal that the task is complete and return the result to the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "result": {"type": "string", "description": "Final result or answer for the user"},
                },
                "required": ["result"],
            },
        },
    },
]
