"""One browser instruction; launching the browser belongs to PowerShell."""


def instruction(url: str) -> None:
    print("+--------------------------------------------------+")
    print("Open this in your browser (Ctrl+click):")
    print(url)
    print("+--------------------------------------------------+", flush=True)
