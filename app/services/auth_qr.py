"""Shared presentation of native engine authorization URLs."""


def qr_png(url):
    import cv2
    matrix = cv2.QRCodeEncoder_create().encode(url)
    matrix = cv2.copyMakeBorder(matrix, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=255)
    scale = max(4, 288 // max(1, int(matrix.shape[0])))
    matrix = cv2.resize(matrix, (int(matrix.shape[1]) * scale, int(matrix.shape[0]) * scale), interpolation=cv2.INTER_NEAREST)
    success, encoded = cv2.imencode('.png', matrix)
    if not success:
        raise RuntimeError('二维码编码失败')
    return encoded.tobytes()
