"""
tcp_receiver_node.py
Nodo ROS2 Jazzy que recibe datos por socket TCP y los publica
en un topic ROS2 como std_msgs/String.

Soporta múltiples clientes simultáneos: cada conexión entrante
recibe su propio hilo de recepción.

Autor: Juan
Puerto TCP por defecto: 5005
Topic de salida:        /tcp_data
"""

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

import socket
import threading


class TcpReceiverNode(Node):
    """
    Nodo ROS2 que actúa como servidor TCP.

    Arquitectura de hilos:
      - Hilo principal  : rclpy.spin() → maneja callbacks ROS2
      - Hilo accept     : espera conexiones entrantes de clientes
      - Hilo por cliente: uno por cada cliente conectado, hace recv()

    Parámetros ROS2 configurables:
      - tcp_port    (int) : puerto donde escuchar         (default 5005)
      - tcp_host    (str) : dirección de bind             (default '0.0.0.0')
      - buffer_size (int) : tamaño del buffer en bytes    (default 4096)
      - max_clients (int) : máx. conexiones en cola       (default 5)
    """

    def __init__(self):
        super().__init__('tcp_receiver_node')

        # ── Declarar parámetros ROS2 ──────────────────────────────────
        self.declare_parameter('tcp_port',    5005)
        self.declare_parameter('tcp_host',    '0.0.0.0')
        self.declare_parameter('buffer_size', 4096)
        self.declare_parameter('max_clients', 5)

        # ── Leer parámetros ───────────────────────────────────────────
        self.tcp_port    = self.get_parameter('tcp_port').value
        self.tcp_host    = self.get_parameter('tcp_host').value
        self.buffer_size = self.get_parameter('buffer_size').value
        self.max_clients = self.get_parameter('max_clients').value

        # ── Publisher ─────────────────────────────────────────────────
        self.publisher_ = self.create_publisher(String, '/tcp_data', 10)

        # ── Lock para publicación thread-safe ─────────────────────────
        # Aunque publish() en rclpy es thread-safe en la mayoría de
        # casos, usar un lock es la práctica recomendada cuando
        # múltiples hilos pueden llamar a publish() al mismo tiempo.
        self._publish_lock = threading.Lock()

        # ── Lista de hilos de clientes activos ────────────────────────
        self._client_threads: list[threading.Thread] = []
        self._clients_lock = threading.Lock()

        # ── Crear socket TCP servidor ─────────────────────────────────
        # AF_INET   = IPv4
        # SOCK_STREAM = TCP (orientado a conexión, confiable)
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        # SO_REUSEADDR: permite reiniciar el nodo sin esperar TIME_WAIT
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # Timeout en accept() para poder verificar _running periódicamente
        self.server_sock.settimeout(1.0)

        try:
            self.server_sock.bind((self.tcp_host, self.tcp_port))
            # listen(max_clients): cuántas conexiones puede encolar el SO
            # antes de que el nodo llame a accept()
            self.server_sock.listen(self.max_clients)
            self.get_logger().info(
                f'Servidor TCP escuchando en {self.tcp_host}:{self.tcp_port}'
            )
        except OSError as e:
            self.get_logger().error(f'No se pudo abrir el socket TCP: {e}')
            raise

        # ── Bandera de control ────────────────────────────────────────
        self._running = True

        # ── Hilo de accept (espera conexiones entrantes) ──────────────
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            daemon=True,
            name='tcp_accept_thread'
        )
        self._accept_thread.start()

        self.get_logger().info(
            f'TcpReceiverNode iniciado. '
            f'Publicando en /tcp_data | '
            f'Puerto TCP: {self.tcp_port}'
        )

    # ─────────────────────────────────────────────────────────────────
    # Bucle de accept (hilo dedicado)
    # ─────────────────────────────────────────────────────────────────
    def _accept_loop(self):
        """
        Espera conexiones TCP entrantes indefinidamente.

        Por cada cliente que se conecta, lanza un hilo separado
        (_client_loop) para manejar su comunicación, y vuelve
        inmediatamente a esperar la siguiente conexión.
        """
        while self._running:
            try:
                # Bloquea hasta que un cliente se conecte o el
                # timeout de 1s expire (para revisar _running)
                client_sock, addr = self.server_sock.accept()

                self.get_logger().info(
                    f'Cliente conectado: {addr[0]}:{addr[1]}'
                )

                # Timeout en recv() del cliente para permitir
                # cierre limpio cuando _running sea False
                client_sock.settimeout(1.0)

                # Lanzar hilo dedicado a este cliente
                t = threading.Thread(
                    target=self._client_loop,
                    args=(client_sock, addr),
                    daemon=True,
                    name=f'tcp_client_{addr[0]}_{addr[1]}'
                )
                t.start()

                # Registrar el hilo para limpieza posterior
                with self._clients_lock:
                    # Limpiar hilos que ya terminaron antes de agregar
                    self._client_threads = [
                        th for th in self._client_threads if th.is_alive()
                    ]
                    self._client_threads.append(t)

            except socket.timeout:
                # Timeout normal del accept: revisar _running y continuar
                continue

            except OSError:
                # Socket cerrado (nodo destruido), salir limpio
                if self._running:
                    self.get_logger().error(
                        'Socket servidor cerrado inesperadamente.'
                    )
                break

    # ─────────────────────────────────────────────────────────────────
    # Bucle de recepción por cliente (hilo por conexión)
    # ─────────────────────────────────────────────────────────────────
    def _client_loop(self, client_sock: socket.socket, addr: tuple):
        """
        Maneja la comunicación con un cliente TCP conectado.

        Llama a recv() en un bucle hasta que el cliente cierra
        la conexión (recv retorna bytes vacíos) o hay un error.

        Args:
            client_sock : socket de la conexión aceptada
            addr        : tupla (ip, puerto) del cliente
        """
        ip, port = addr

        try:
            while self._running:
                try:
                    # recv() retorna b'' cuando el cliente cierra la conexión
                    data = client_sock.recv(self.buffer_size)

                    if not data:
                        # El cliente cerró la conexión limpiamente
                        self.get_logger().info(
                            f'Cliente desconectado: {ip}:{port}'
                        )
                        break

                    # Decodificar y publicar
                    message = data.decode('utf-8').strip()

                    self.get_logger().debug(
                        f'Datos de {ip}:{port} → "{message}"'
                    )

                    self._publish_data(message, addr)

                except socket.timeout:
                    # Timeout de recv: revisar _running y continuar
                    continue

                except UnicodeDecodeError as e:
                    self.get_logger().warn(
                        f'Error de decodificación de {ip}:{port}: {e}. '
                        f'Mensaje ignorado.'
                    )

        except OSError as e:
            if self._running:
                self.get_logger().warn(
                    f'Error de socket con cliente {ip}:{port}: {e}'
                )
        finally:
            # Cerrar el socket del cliente siempre, pase lo que pase
            try:
                client_sock.close()
            except OSError:
                pass
            self.get_logger().info(f'Socket de {ip}:{port} cerrado.')

    # ─────────────────────────────────────────────────────────────────
    # Publicar en el topic ROS2 (thread-safe)
    # ─────────────────────────────────────────────────────────────────
    def _publish_data(self, message: str, addr: tuple):
        """
        Construye un std_msgs/String y lo publica en /tcp_data.

        Usa un lock para garantizar que múltiples hilos de cliente
        no publiquen al mismo tiempo de forma no controlada.

        Args:
            message : string decodificado del paquete TCP
            addr    : tupla (ip, puerto) del remitente
        """
        msg = String()
        msg.data = f'[{addr[0]}:{addr[1]}] {message}'

        with self._publish_lock:
            self.publisher_.publish(msg)

        self.get_logger().info(f'Publicado: {msg.data}')

    # ─────────────────────────────────────────────────────────────────
    # Limpieza al destruir el nodo
    # ─────────────────────────────────────────────────────────────────
    def destroy_node(self):
        """
        Detiene todos los hilos y cierra el socket servidor.

        ROS2 llama a este método automáticamente cuando se hace
        rclpy.shutdown() o se presiona Ctrl+C.
        """
        self.get_logger().info('Cerrando TcpReceiverNode...')

        self._running = False

        # Cerrar el socket servidor: desbloquea el accept() bloqueado
        try:
            self.server_sock.close()
        except OSError:
            pass

        # Esperar el hilo de accept
        self._accept_thread.join(timeout=2.0)

        # Esperar los hilos de clientes activos
        with self._clients_lock:
            for t in self._client_threads:
                t.join(timeout=2.0)

        super().destroy_node()


# ─────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────
def main(args=None):
    rclpy.init(args=args)

    node = TcpReceiverNode()

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        node.get_logger().info('Interrupción por teclado (Ctrl+C).')
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()