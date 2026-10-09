# Security

ChatBridge works only on local files and makes no network requests. It reads and writes chat history, which can contain secrets that appeared in conversations, so treat its backups, journals (`~/.local/share/chatbridge/journal`, or `%LOCALAPPDATA%\ChatBridge\journal` on Windows) and logs accordingly.

Please report vulnerabilities privately to the maintainers (use the repository's private vulnerability reporting under the Security tab) instead of opening a public issue. Include the version (`chatbridge doctor`), and never attach real chat content.
