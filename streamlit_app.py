"""Entry point for Streamlit Community Cloud (and `streamlit run streamlit_app.py`). Shows the demo only;
run `python -m payrecon.recover dashboard` on your own machine to see your own data."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))  # works without installing the package

from payrecon.dashboard_app import main  # noqa: E402

main()
