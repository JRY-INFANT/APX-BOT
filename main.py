import os
import sys

# Entry point for hosts running main.py (such as Bot-Hosting.net)
if __name__ == "__main__":
    # If alpha-x.py is present, run the Discord bot
    if os.path.exists("alpha-x.py"):
        with open("alpha-x.py", "rb") as f:
            exec(compile(f.read(), "alpha-x.py", "exec"), globals())
    else:
        try:
            from api.index import handler
        except ImportError:
            pass
else:
    # If imported by Vercel serverless
    try:
        from api.index import handler
    except ImportError:
        pass
