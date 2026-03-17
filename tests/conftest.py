"""
Pytest configuration for stock quantitative system tests.
This file is automatically discovered by pytest and runs before any tests.
"""
import os
import sys
# Add project root to Python path so we can import src modules
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
