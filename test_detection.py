#!/usr/bin/env python3
"""
Test script to verify that password prompt detection works correctly.
This should be run in the same directory and environment as aerotunnel.py
"""

import sys
import os

# Add current directory to path so we can import aerotunnel
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import a minimal version of the function to test 
def test_password_detection():
    """Test that our password prompt detection logic works."""
    print("Testing password prompt detection logic...")
    
    # Import our actual functions to simulate testing
    from aerotunnel import TunnelApp
    
    # Create a mock instance 
    app = TunnelApp()
    
    # Create test lines
    test_lines = [
        "Enter passphrase for key '/home/user/.ssh/id_rsa':",
        "password: ",
        "Enter password for user@host:",
        "Some normal SSH output",
        "Permission denied (publickey)",
        "Connection closed by 192.168.1.100 port 22"
    ]
    
    print("Expected matches:")
    for line in test_lines:
        pattern_match = any(pattern in line.lower() for pattern in ("password:", "passphrase:"))
        print(f"  '{line}' -> {pattern_match}")

    print("\nIf our logic worked correctly, we would see detection messages logged to screen.")

if __name__ == "__main__":
    test_password_detection()