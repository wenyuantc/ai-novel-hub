"""部署示例：通过 stdio 返回调用方所给文本的非空白字符数。"""

from mcp.server.fastmcp import FastMCP

server = FastMCP("manuscript-stats")


@server.tool()
def count_characters(text: str) -> str:
    """统计文本的非空白字符数。"""
    count = sum(not character.isspace() for character in text)
    return str(count)


if __name__ == "__main__":
    server.run(transport="stdio")
