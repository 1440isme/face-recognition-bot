import cv2
import numpy as np
import face_recognition
import os
import time
import threading
import logging
from gpiozero import Servo, Buzzer
from gpiozero.pins.pigpio import PiGPIOFactory
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Updater, CallbackQueryHandler, CallbackContext
from flask import Flask, Response
from config import BOT_TOKEN, CHAT_ID

# Tắt logging Flask
logging.getLogger('werkzeug').disabled = True

# GPIO setup
factory = PiGPIOFactory()
servo = Servo(17, pin_factory=factory, min_pulse_width=0.5/1000, max_pulse_width=2.5/1000)
buzzer = Buzzer(27)

def go_to_angle(angle):
    value = (angle - 90) / 90
    print(f"→ Quay den {angle}° (servo.value = {value:.2f})")
    servo.value = value

go_to_angle(77)  # Đóng cửa

# Load known face
img = face_recognition.load_image_file("known_faces/owner.jpg")
owner_encoding = face_recognition.face_encodings(img)[0]

# Telegram setup
bot = Bot(BOT_TOKEN)
pending_unlock = False
confirmation_result = None
system_state = "idle"
frame_lock = threading.Lock()
output_frame = None

# Flask setup
app = Flask(__name__)
@app.route("/stream")
def stream():
    def generate():
        global output_frame
        while True:
            with frame_lock:
                if output_frame is None:
                    continue
                _, jpeg = cv2.imencode('.jpg', output_frame)
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")

def flask_thread():
    app.run(host='0.0.0.0', port=5000)

# Gửi cảnh báo Telegram
def send_alert(photo_path):
    global pending_unlock, system_state
    keyboard = [[InlineKeyboardButton("Mở cửa", callback_data='open')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    with open(photo_path, 'rb') as photo:
        bot.send_photo(chat_id=CHAT_ID, photo=photo, caption="Phát hiện người lạ", reply_markup=reply_markup)
    pending_unlock = True
    system_state = "awaiting"

# Callback xác nhận Telegram
def telegram_callback(update: Update, context: CallbackContext):
    global pending_unlock, confirmation_result, system_state
    query = update.callback_query
    query.answer()

    if query.data == 'open':
        bot.send_message(chat_id=CHAT_ID, text="Đã mở cửa")
        confirmation_result = True
    else:
        bot.send_message(chat_id=CHAT_ID, text="Từ chối mở cửa. Cảnh báo kích hoạt.")
        confirmation_result = False

    pending_unlock = False
    system_state = "idle"

def run_telegram_bot():
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CallbackQueryHandler(telegram_callback))
    updater.start_polling()

# Mở cửa
def open_door():
    go_to_angle(125)
    time.sleep(5)
    go_to_angle(77)

# Đợi xác nhận
def wait_for_response(timeout=5):
    global pending_unlock, confirmation_result, system_state
    confirmation_result = None
    waited = 0
    while confirmation_result is None and waited < timeout:
        time.sleep(1)
        waited += 1

    if confirmation_result is None:
        print("→ Không có xác nhận → Cảnh báo")
        buzzer.on()
        time.sleep(3)
        buzzer.off()
        confirmed = False
    else:
        confirmed = confirmation_result

    pending_unlock = False
    confirmation_result = None
    system_state = "idle"
    return confirmed

# Nhận diện khuôn mặt
def recognize_loop():
    global output_frame, pending_unlock, system_state
    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)

    cap.set(cv2.CAP_PROP_FPS, 10)

    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))

    skip_counter = 0
    owner_detected = False
    stranger_detected = False

    while True:
        ret, frame = cap.read()
        if not ret:
            continue

        small_frame = cv2.resize(frame, (0, 0), fx=0.5, fy=0.5)

        gray = cv2.cvtColor(small_frame, cv2.COLOR_BGR2GRAY)

        rgb = cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB)
        with frame_lock:
            output_frame = frame.copy()

        if skip_counter < 5:
            skip_counter += 1
            continue
        skip_counter = 0

        face_locations = face_recognition.face_locations(rgb)
        if not face_locations or system_state != "idle":
            owner_detected = False
            stranger_detected = False
            continue

        face_encodings = face_recognition.face_encodings(rgb, face_locations)

        for encoding, (top, right, bottom, left) in zip(face_encodings, face_locations):
            match = face_recognition.compare_faces([owner_encoding], encoding)[0]
            if match:
                if not owner_detected:
                    print("→ Chủ nhà, đang mở cửa...")
                    system_state = "busy"
                    open_door()
                    system_state = "idle"
                    owner_detected = True
                    stranger_detected = False
            else:
                if not stranger_detected:
                    print("→ Người lạ, gửi ảnh lên Telegram")
                    filename = f"unknown_faces/face_{int(time.time())}.jpg"
                    cv2.imwrite(filename, frame)
                    send_alert(filename)
                    confirmed = wait_for_response()

                    if confirmed:
                        print("→ Mở cửa thành công từ Telegram")
                        system_state = "busy"
                        open_door()
                        system_state = "idle"
                    else:
                        print("→ Có người lạ không xác nhận")

                    stranger_detected = True
                    owner_detected = False

        time.sleep(0.05)

if __name__ == "__main__":
    os.makedirs("unknown_faces", exist_ok=True)
    print("He thong mo cua da san sang...")

    threading.Thread(target=flask_thread, daemon=True).start()
    threading.Thread(target=run_telegram_bot, daemon=True).start()

    recognize_loop()

