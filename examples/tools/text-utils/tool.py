from mcp.server.fastmcp import FastMCP

server = FastMCP("Text Utilities")


@server.tool()
def word_count(text: str) -> dict:
    """统计文本字符数与单词数。"""
    return {"characters": len(text), "words": len(text.split())}


@server.tool()
def uppercase(text: str) -> str:
    """将文本转换为大写。"""
    return text.upper()


if __name__ == "__main__":
    server.run(transport="stdio")
