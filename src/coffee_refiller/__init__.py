def main() -> None:
    """Run the dashboard at http://127.0.0.1:8000."""
    import uvicorn

    uvicorn.run("coffee_refiller.app:app", host="127.0.0.1", port=8000)
