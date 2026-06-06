import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image
from cv_bridge import CvBridge
import subprocess
import cv2
import os
import tempfile


class CameraNode(Node):
    def __init__(self):
        super().__init__('camera_node')
        self.publisher_ = self.create_publisher(Image, 'camera/image_raw', 10)
        self.timer = self.create_timer(1.0, self.timer_callback)
        self.bridge = CvBridge()
        self.temp_path = os.path.join(tempfile.gettempdir(), 'rpi_cam_frame.jpg')

        # Archivo único que se sobreescribe siempre
        self.save_path = os.path.expanduser('~/captura.jpg')

        self.get_logger().info(f'Camera node iniciado. Imagen en {self.save_path}')

    def timer_callback(self):
        try:
            result = subprocess.run([
                'rpicam-still',
                '--nopreview',
                '--width', '640',
                '--height', '480',
                '-t', '100',
                '-o', self.temp_path
            ], capture_output=True, timeout=5)

            if result.returncode != 0:
                self.get_logger().error(f'Error capturando imagen: {result.stderr.decode()}')
                return

            frame = cv2.imread(self.temp_path)
            if frame is None:
                self.get_logger().error('No se pudo leer la imagen capturada')
                return

            # Sobreescribe siempre el mismo archivo
            cv2.imwrite(self.save_path, frame)

            msg = self.bridge.cv2_to_imgmsg(frame, encoding='bgr8')
            msg.header.stamp = self.get_clock().now().to_msg()
            msg.header.frame_id = 'camera_frame'
            self.publisher_.publish(msg)
            self.get_logger().info('Imagen actualizada')

        except subprocess.TimeoutExpired:
            self.get_logger().error('Timeout esperando rpicam-still')
        except Exception as e:
            self.get_logger().error(f'Error inesperado: {str(e)}')


def main(args=None):
    rclpy.init(args=args)
    node = CameraNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()