#!/bin/sh
set -eu

MEDIAMTX_HOST="${MEDIAMTX_HOST:-mediamtx}"

ffmpeg -f v4l2 -i /dev/video0 \
  -c:v libx264 -preset veryfast -tune zerolatency \
  -vf scale=1280:720 -r 20 \
  -f rtsp "rtsp://${MEDIAMTX_HOST}:8554/cam_salon_main" &

ffmpeg -f v4l2 -i /dev/video0 \
  -c:v libx264 -preset veryfast -tune zerolatency \
  -vf scale=640:360 -r 8 \
  -f rtsp "rtsp://${MEDIAMTX_HOST}:8554/cam_salon_sub" &

wait -n
