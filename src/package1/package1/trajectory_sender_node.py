#!/usr/bin/env python3
"""
trajectory_sender_node.py

Nodo ROS2 Jazzy para enviar trayectorias a la ESP32 via el topic esp32/cmd_vel,
con control cinemático inverso diferencial (feedforward + corrección proporcional
en espacio articular).

Uso:
  Publicar un Int32 al topic /trajectory_set con valor 1, 2 o 3
  para ejecutar el set correspondiente.

Sets:
  Set 1: T1, T2
  Set 2: T3, T4, T5, T6
  Set 3: T7, T8, T9, T10

Archivos requeridos por trayectoria (en TRAJ_DIR):
  - T{n}.csv   → posiciones deseadas   (columnas: iteracion, q1, q2)
  - T{n}_p.csv → velocidades deseadas  (columnas: iteracion, q1_p, q2_p)

Cada trayectoria tiene 51 iteraciones. Las velocidades se envían cada 20ms.

Control cinemático diferencial:
  q_p_cmd = q_p_feedforward + Kp * (q_des - q_real)

  donde:
    q_des  = posición deseada en la iteración actual (del CSV T{n}.csv)
    q_real = posición real de las articulaciones (del topic esp32/state, JointState)
    q_p_ff = velocidad feedforward (del CSV T{n}_p.csv)
    Kp     = ganancia proporcional (ajustable en KP)

  Si no ha llegado ningún mensaje de feedback aún, se opera en lazo abierto
  publicando únicamente el feedforward.
"""

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile

from std_msgs.msg import Int32
from geometry_msgs.msg import Vector3
from sensor_msgs.msg import JointState

import csv
import os
import threading


# ─────────────────────────────────────────────────────────────────────────────
# Constantes de configuración
# ─────────────────────────────────────────────────────────────────────────────

TRAJECTORY_SETS = {
    1: [1, 2],
    2: [3, 4, 5, 6],
    3: [7, 8, 9, 10],
}

TRAJ_DIR           = '/home/camilo/trayectorias'
PUBLISH_INTERVAL_S = 0.02   # 20 ms entre iteraciones
TOTAL_ITERATIONS   = 51     # Iteraciones esperadas por trayectoria

# Ganancia proporcional del controlador cinemático.
# Unidades: (rad/s) / rad  →  1/s
# Aumentar si la corrección es lenta; bajar si hay oscilaciones.
KP = 5.0

# Topic donde el nodo ESP32 publica el estado real de las articulaciones.
# Tipo: sensor_msgs/JointState
# position[0] = q1_real (rad)   position[1] = q2_real (rad)
FEEDBACK_TOPIC = 'esp32/state'


# ─────────────────────────────────────────────────────────────────────────────
# Nodo principal
# ─────────────────────────────────────────────────────────────────────────────

class TrajectorySenderNode(Node):
    """
    Nodo que recibe un número de set (1/2/3) por topic y publica
    las velocidades corregidas de cada trayectoria hacia esp32/cmd_vel.

    Ley de control por iteración:
        e_q      = q_des - q_real          (error articular)
        q_p_cmd  = q_p_ff + Kp * e_q      (feedforward + corrección)
    """

    def __init__(self):
        super().__init__('trajectory_sender_node')

        qos = QoSProfile(depth=10)

        # ── Subscriber: número de set a ejecutar ──────────────────────────────
        self.set_sub = self.create_subscription(
            Int32,
            '/trajectory_set',
            self.set_callback,
            qos
        )

        # ── Subscriber: estado real de articulaciones desde la ESP32 ──────────
        # El nodo esp32_serial_node publica en 'esp32/state' un JointState con:
        #   name       = ['motor1', 'motor2']
        #   position   = [p1, p2]   ← q1_real, q2_real en radianes
        #   velocity   = [v1, v2]   ← velocidades reales (no usadas en control)
        #
        # Se protege con Lock porque el callback de ROS2 (ejecutado por el
        # executor en el hilo principal) y el hilo de ejecución de trayectorias
        # acceden a _q_real de forma concurrente.
        self._feedback_lock     = threading.Lock()
        self._q_real: tuple[float, float] | None = None
        self._feedback_received = False

        self.feedback_sub = self.create_subscription(
            JointState,
            FEEDBACK_TOPIC,
            self._feedback_callback,
            qos
        )

        # ── Publisher: velocidades corregidas hacia la ESP32 ──────────────────
        self.cmd_pub = self.create_publisher(
            Vector3,
            'esp32/cmd_vel',
            qos
        )

        # ── Estado de ejecución de trayectorias ───────────────────────────────
        self._running     = False
        self._stop_event  = threading.Event()
        self._exec_thread: threading.Thread | None = None

        self.get_logger().info(
            'TrajectorySenderNode iniciado.\n'
            f'  Feedback topic : {FEEDBACK_TOPIC}  (sensor_msgs/JointState)\n'
            f'  Kp             : {KP}\n'
            f'  Intervalo      : {PUBLISH_INTERVAL_S * 1000:.0f} ms\n'
            'Publica en /trajectory_set (Int32) el número de set (1, 2 o 3).'
        )

    # =========================================================================
    # CALLBACK: estado real desde la ESP32  (esp32/state → JointState)
    # =========================================================================
    def _feedback_callback(self, msg: JointState):
        """
        Actualiza la posición real de las articulaciones.

        El nodo esp32_serial_node llena el JointState así:
            msg.position = [p1, p2]    donde p1=q1_real, p2=q2_real

        Se valida que el mensaje tenga al menos 2 posiciones antes de usarlo.
        """
        if len(msg.position) < 2:
            self.get_logger().warn(
                f'JointState recibido con menos de 2 posiciones '
                f'({len(msg.position)}). Ignorando.'
            )
            return

        with self._feedback_lock:
            self._q_real = (msg.position[0], msg.position[1])
            self._feedback_received = True

    def _get_q_real(self) -> tuple[float, float] | None:
        """Devuelve la última posición real recibida de forma thread-safe."""
        with self._feedback_lock:
            return self._q_real

    # =========================================================================
    # CALLBACK: número de set recibido  (/trajectory_set → Int32)
    # =========================================================================
    def set_callback(self, msg: Int32):
        set_id = msg.data

        if set_id not in TRAJECTORY_SETS:
            self.get_logger().error(
                f'Set "{set_id}" no válido. Usa 1, 2 o 3.')
            return

        # Si ya hay un set en ejecución, interrumpirlo limpiamente
        if self._running:
            self.get_logger().warn(
                f'Interrumpiendo set en curso para iniciar Set {set_id}.')
            self._stop_event.set()
            if self._exec_thread is not None:
                self._exec_thread.join(timeout=2.0)

        # Lanzar nuevo hilo de ejecución
        self._stop_event.clear()
        self._running = True
        self._exec_thread = threading.Thread(
            target=self._run_set,
            args=(set_id,),
            daemon=True
        )
        self._exec_thread.start()

    # =========================================================================
    # EJECUCIÓN DEL SET (corre en hilo separado para no bloquear el executor)
    # =========================================================================
    def _run_set(self, set_id: int):
        traj_indices = TRAJECTORY_SETS[set_id]
        self.get_logger().info(
            f'▶ Iniciando Set {set_id} | '
            f'Trayectorias: {["T" + str(i) for i in traj_indices]}'
        )

        for traj_idx in traj_indices:
            if self._stop_event.is_set():
                self.get_logger().warn('Set interrumpido antes de completarse.')
                break

            # ── Rutas de los archivos CSV ──────────────────────────────────────
            pos_file = os.path.join(TRAJ_DIR, f'T{traj_idx}.csv')    # posiciones
            vel_file = os.path.join(TRAJ_DIR, f'T{traj_idx}_p.csv')  # velocidades

            # ── Cargar ambos CSVs antes de empezar ────────────────────────────
            try:
                positions  = self._load_positions(pos_file)
                velocities = self._load_velocities(vel_file)
            except FileNotFoundError as e:
                self.get_logger().error(
                    f'Archivo no encontrado: {e}. Abortando set.')
                break
            except ValueError as e:
                self.get_logger().error(
                    f'Error de formato en archivos de T{traj_idx}: {e}. Abortando set.')
                break
            except Exception as e:
                self.get_logger().error(
                    f'Error inesperado cargando T{traj_idx}: {e}. Abortando set.')
                break

            # ── Advertir si el número de filas no es el esperado ──────────────
            if len(positions) != TOTAL_ITERATIONS:
                self.get_logger().warn(
                    f'T{traj_idx}.csv tiene {len(positions)} filas '
                    f'(se esperaban {TOTAL_ITERATIONS}).')
            if len(velocities) != TOTAL_ITERATIONS:
                self.get_logger().warn(
                    f'T{traj_idx}_p.csv tiene {len(velocities)} filas '
                    f'(se esperaban {TOTAL_ITERATIONS}).')

            # Usar el mínimo para no salirse de índice si los CSV tienen distinto largo
            n_iters = min(len(positions), len(velocities))

            self.get_logger().info(
                f'  → Ejecutando T{traj_idx} '
                f'({n_iters} iteraciones × {PUBLISH_INTERVAL_S * 1000:.0f} ms)'
            )

            # ── Advertir si aún no hay feedback de la ESP32 ───────────────────
            if not self._feedback_received:
                self.get_logger().warn(
                    f'Aún no se ha recibido feedback de "{FEEDBACK_TOPIC}". '
                    'Ejecutando en lazo abierto (solo feedforward) hasta que llegue.'
                )

            # ── Bucle de control: una iteración cada 20 ms ────────────────────
            for i in range(n_iters):
                if self._stop_event.is_set():
                    self.get_logger().warn(
                        f'T{traj_idx} interrumpida en iteración {i + 1}.')
                    break

                # Referencia de esta iteración
                q1_des,  q2_des   = positions[i]
                q1_p_ff, q2_p_ff  = velocities[i]

                # Posición real (puede ser None si aún no llega feedback)
                q_real = self._get_q_real()

                if q_real is not None:
                    q1_real, q2_real = q_real

                    # ── Ley de control cinemático diferencial ─────────────────
                    # Error articular (espacio de configuración)
                    e_q1 = q1_des - q1_real
                    e_q2 = q2_des - q2_real

                    # Velocidad comandada = feedforward + corrección proporcional
                    # El Jacobiano en espacio articular es la identidad (J = I),
                    # por lo que la corrección diferencial se reduce a Kp * e_q.
                    q1_p_cmd = q1_p_ff + KP * e_q1
                    q2_p_cmd = q2_p_ff + KP * e_q2

                    self.get_logger().debug(
                        f'T{traj_idx} | iter {i + 1:02d} | '
                        f'des=({q1_des:.4f}, {q2_des:.4f}) | '
                        f'real=({q1_real:.4f}, {q2_real:.4f}) | '
                        f'err=({e_q1:.4f}, {e_q2:.4f}) | '
                        f'cmd=({q1_p_cmd:.4f}, {q2_p_cmd:.4f})'
                    )

                else:
                    # ── Lazo abierto: sin feedback disponible ─────────────────
                    q1_p_cmd = q1_p_ff
                    q2_p_cmd = q2_p_ff

                    self.get_logger().debug(
                        f'T{traj_idx} | iter {i + 1:02d} | '
                        f'[lazo abierto] '
                        f'cmd=({q1_p_cmd:.4f}, {q2_p_cmd:.4f})'
                    )

                # ── Publicar velocidad corregida → esp32/cmd_vel ───────────────
                vec_msg = Vector3()
                vec_msg.x = q1_p_cmd
                vec_msg.y = q2_p_cmd
                vec_msg.z = 0.0
                self.cmd_pub.publish(vec_msg)

                # Esperar 20 ms; sale antes si _stop_event se activa
                interrupted = self._stop_event.wait(timeout=PUBLISH_INTERVAL_S)
                if interrupted:
                    break

            else:
                # El bucle for terminó sin break → trayectoria completada
                self.get_logger().info(f'  ✓ T{traj_idx} completada.')

        if not self._stop_event.is_set():
            self.get_logger().info(f'✅ Set {set_id} completado.')

        self._running = False

    # =========================================================================
    # CARGA DE CSV DE POSICIONES DESEADAS  (T{n}.csv)
    # =========================================================================
    def _load_positions(self, filepath: str) -> list[tuple[float, float]]:
        """
        Lee el CSV de posiciones deseadas y devuelve lista de (q1, q2)
        ordenada por columna 'iteracion'.

        Formato esperado:
            iteracion,q1,q2
            1,0.0,0.0
            2,0.01,0.02
            ...
        """
        positions = []

        with open(filepath, newline='', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile)

            required_cols = {'iteracion', 'q1', 'q2'}
            if not required_cols.issubset(set(reader.fieldnames or [])):
                raise ValueError(
                    f'Columnas requeridas {required_cols} no encontradas en '
                    f'{filepath}. Columnas presentes: {reader.fieldnames}'
                )

            rows = sorted(reader, key=lambda r: int(r['iteracion']))

            for row in rows:
                positions.append((float(row['q1']), float(row['q2'])))

        return positions

    # =========================================================================
    # CARGA DE CSV DE VELOCIDADES FEEDFORWARD  (T{n}_p.csv)
    # =========================================================================
    def _load_velocities(self, filepath: str) -> list[tuple[float, float]]:
        """
        Lee el CSV de velocidades feedforward y devuelve lista de (q1_p, q2_p)
        ordenada por columna 'iteracion'.

        Formato esperado:
            iteracion,q1_p,q2_p
            1,0,0
            2,0,-0.062832
            ...
        """
        velocities = []

        with open(filepath, newline='', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile)

            required_cols = {'iteracion', 'q1_p', 'q2_p'}
            if not required_cols.issubset(set(reader.fieldnames or [])):
                raise ValueError(
                    f'Columnas requeridas {required_cols} no encontradas en '
                    f'{filepath}. Columnas presentes: {reader.fieldnames}'
                )

            rows = sorted(reader, key=lambda r: int(r['iteracion']))

            for row in rows:
                velocities.append((float(row['q1_p']), float(row['q2_p'])))

        return velocities


# =============================================================================
# MAIN
# =============================================================================
def main(args=None):
    rclpy.init(args=args)
    node = TrajectorySenderNode()

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        # Detener cualquier trayectoria en ejecución antes de salir
        node._stop_event.set()
        if node._exec_thread is not None:
            node._exec_thread.join(timeout=2.0)
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()