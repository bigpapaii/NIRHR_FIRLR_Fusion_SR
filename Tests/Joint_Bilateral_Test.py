import numpy as np
import cv2
import time

# Create a random small image (120x160)
small = (np.random.rand(120, 160, 3) * 255).astype(np.uint8)

# Upscale to 1280x720
large = cv2.resize(small, (1280, 720), interpolation=cv2.INTER_LINEAR)

# Use the upscaled image as both source and guidance (common baseline case)
src = large.copy()
guide = large.copy()

# Parameters for joint bilateral filter
d = 5
sigmaColor = 25
sigmaSpace = 7

# Time the operation
start = time.time()

# Some OpenCV builds use ximgproc for joint bilateral filter
try:
    filtered = cv2.ximgproc.jointBilateralFilter(guide, src, d, sigmaColor, sigmaSpace)
    method = "cv2.ximgproc.jointBilateralFilter"
except AttributeError:
    # fallback: approximate with bilateralFilter (not joint, but still meaningful)
    filtered = cv2.bilateralFilter(src, d, sigmaColor, sigmaSpace)
    method = "cv2.bilateralFilter (fallback)"

end = time.time()

elapsed = end - start

method, large.shape, elapsed

print(f"Method: {method}, Output Shape: {filtered.shape}, Time Elapsed: {elapsed:.4f} seconds")