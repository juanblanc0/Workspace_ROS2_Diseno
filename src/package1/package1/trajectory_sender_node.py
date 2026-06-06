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

Control idle (sin trayectoria activa):
  q_p_cmd = KP_IDLE * (0.0 - q_real)
  setpoint publicado: (0.0, 0.0)
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
PUBLISH_INTERVAL_S = 0.02
TOTAL_ITERATIONS   = 51
KP                 = 5.0
KP_IDLE            = 5.0
KP_MAX_CORRECTION  = 1.5
FEEDBACK_TOPIC     = 'esp32/state'


# ─────────────────────────────────────────────────────────────────────────────
# Nodo principal
# ─────────────────────────────────────────────────────────────────────────────

class TrajectorySenderNode(Node):

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

        # ── Publisher: setpoint de posición deseada (MODIFICACIÓN MÍNIMA) ─────
        # Publica (q1_des, q2_des, 0) en cada iteración de trayectoria.
        # En idle publica (0, 0, 0).
        # Lo consume data_logger_node para graficar real vs deseado.
        self.setpoint_pub = self.create_publisher(
            Vector3,
            '/traj_setpoint',
            qos
        )

        # ── Estado de ejecución ───────────────────────────────────────────────
        self._running     = False
        self._stop_event  = threading.Event()
        self._exec_thread: threading.Thread | None = None

        # ── Timer de control idle ─────────────────────────────────────────────
        self._idle_timer = self.create_timer(
            PUBLISH_INTERVAL_S,
            self._idle_control_callback
        )

        self.get_logger().info(
            'TrajectorySenderNode iniciado.\n'
            f'  Feedback topic    : {FEEDBACK_TOPIC}  (sensor_msgs/JointState)\n'
            f'  Kp (trayectoria)  : {KP}\n'
            f'  Kp (idle)         : {KP_IDLE}\n'
            f'  KP_MAX_CORRECTION : {KP_MAX_CORRECTION} rad/s\n'
            f'  Intervalo         : {PUBLISH_INTERVAL_S * 1000:.0f} ms\n'
            'Publica en /trajectory_set (Int32) el número de set (1, 2 o 3).'
        )

    # =========================================================================
    # IDLE CONTROL
    # =========================================================================
    def _idle_control_callback(self):
        if self._running:
            return

        q_real = self._get_q_real()

        # Setpoint de posición en idle siempre es (0, 0)
        sp_msg = Vector3()
        sp_msg.x = 0.0
        sp_msg.y = 0.0
        sp_msg.z = 0.0
        self.setpoint_pub.publish(sp_msg)

        msg = Vector3()

        if q_real is not None:
            q1_real, q2_real = q_real
            e_q1  = 0.0 - q1_real
            e_q2  = 0.0 - q2_real
            corr1 = max(-KP_MAX_CORRECTION, min(KP_MAX_CORRECTION, KP_IDLE * e_q1))
            corr2 = max(-KP_MAX_CORRECTION, min(KP_MAX_CORRECTION, KP_IDLE * e_q2))
            msg.x = corr1
            msg.y = corr2
            msg.z = 0.0
            self.get_logger().debug(
                f'[IDLE] real=({q1_real:.4f}, {q2_real:.4f}) | '
                f'err=({e_q1:.4f}, {e_q2:.4f}) | '
                f'cmd=({corr1:.4f}, {corr2:.4f})'
            )
        else:
            msg.x = 0.0
            msg.y = 0.0
            msg.z = 0.0
            self.get_logger().debug('[IDLE] Sin feedback aún. Publicando (0, 0, 0).')

        self.cmd_pub.publish(msg)

    # =========================================================================
    # HELPER: stop
    # =========================================================================
    def _publish_stop(self):
        stop_msg = Vector3()
        stop_msg.x = 0.0
        stop_msg.y = 0.0
        stop_msg.z = 0.0
        self.cmd_pub.publish(stop_msg)
        self.get_logger().info('⏹  cmd_vel (0, 0, 0) publicado → robot detenido.')

    # =========================================================================
    # CALLBACK: feedback de la ESP32
    # =========================================================================
    def _feedback_callback(self, msg: JointState):
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
        with self._feedback_lock:
            return self._q_real

    # =========================================================================
    # CALLBACK: número de set
    # =========================================================================
    def set_callback(self, msg: Int32):
        set_id = msg.data

        if set_id not in TRAJECTORY_SETS:
            self.get_logger().error(
                f'Set "{set_id}" no válido. Usa 1, 2 o 3.')
            return

        if self._running:
            self.get_logger().warn(
                f'Interrumpiendo set en curso para iniciar Set {set_id}.')
            self._stop_event.set()
            if self._exec_thread is not None:
                self._exec_thread.join(timeout=2.0)

        self._stop_event.clear()
        self._running = True
        self._exec_thread = threading.Thread(
            target=self._run_set,
            args=(set_id,),
            daemon=True
        )
        self._exec_thread.start()

    # =========================================================================
    # EJECUCIÓN DEL SET
    # =========================================================================
    def _run_set(self, set_id: int):
        traj_indices = TRAJECTORY_SETS[set_id]
        self.get_logger().info(
            f'▶ Iniciando Set {set_id} | '
            f'Trayectorias: {["T" + str(i) for i in traj_indices]}'
        )

        set_aborted = False

        for traj_idx in traj_indices:
            if self._stop_event.is_set():
                self.get_logger().warn('Set interrumpido antes de completarse.')
                set_aborted = True
                break

            pos_file = os.path.join(TRAJ_DIR, f'T{traj_idx}.csv')
            vel_file = os.path.join(TRAJ_DIR, f'T{traj_idx}_p.csv')

            try:
                positions  = self._load_positions(pos_file)
                velocities = self._load_velocities(vel_file)
            except FileNotFoundError as e:
                self.get_logger().error(f'Archivo no encontrado: {e}. Abortando set.')
                set_aborted = True
                break
            except ValueError as e:
                self.get_logger().error(
                    f'Error de formato en archivos de T{traj_idx}: {e}. Abortando set.')
                set_aborted = True
                break
            except Exception as e:
                self.get_logger().error(
                    f'Error inesperado cargando T{traj_idx}: {e}. Abortando set.')
                set_aborted = True
                break

            if len(positions) != TOTAL_ITERATIONS:
                self.get_logger().warn(
                    f'T{traj_idx}.csv tiene {len(positions)} filas '
                    f'(se esperaban {TOTAL_ITERATIONS}).')
            if len(velocities) != TOTAL_ITERATIONS:
                self.get_logger().warn(
                    f'T{traj_idx}_p.csv tiene {len(velocities)} filas '
                    f'(se esperaban {TOTAL_ITERATIONS}).')

            n_iters = min(len(positions), len(velocities))

            self.get_logger().info(
                f'  → Ejecutando T{traj_idx} '
                f'({n_iters} iteraciones × {PUBLISH_INTERVAL_S * 1000:.0f} ms)'
            )

            if not self._feedback_received:
                self.get_logger().warn(
                    f'Aún no se ha recibido feedback de "{FEEDBACK_TOPIC}". '
                    'Ejecutando en lazo abierto (solo feedforward) hasta que llegue.'
                )

            traj_interrupted = False

            for i in range(n_iters):
                if self._stop_event.is_set():
                    self.get_logger().warn(
                        f'T{traj_idx} interrumpida en iteración {i + 1}.')
                    traj_interrupted = True
                    break

                q1_des,  q2_des  = positions[i]
                q1_p_ff, q2_p_ff = velocities[i]

                # ── Publicar setpoint de posición deseada ─────────────────────
                # Es la única modificación dentro del bucle de control.
                sp_msg = Vector3()
                sp_msg.x = q1_des
                sp_msg.y = q2_des
                sp_msg.z = 0.0
                self.setpoint_pub.publish(sp_msg)

                q_real = self._get_q_real()

                if q_real is not None:
                    q1_real, q2_real = q_real
                    e_q1  = q1_des - q1_real
                    e_q2  = q2_des - q2_real
                    corr1 = max(-KP_MAX_CORRECTION, min(KP_MAX_CORRECTION, KP * e_q1))
                    corr2 = max(-KP_MAX_CORRECTION, min(KP_MAX_CORRECTION, KP * e_q2))
                    q1_p_cmd = q1_p_ff + corr1
                    q2_p_cmd = q2_p_ff + corr2
                    self.get_logger().debug(
                        f'T{traj_idx} | iter {i + 1:02d} | '
                        f'des=({q1_des:.4f}, {q2_des:.4f}) | '
                        f'real=({q1_real:.4f}, {q2_real:.4f}) | '
                        f'err=({e_q1:.4f}, {e_q2:.4f}) | '
                        f'corr=({corr1:.4f}, {corr2:.4f}) | '
                        f'cmd=({q1_p_cmd:.4f}, {q2_p_cmd:.4f})'
                    )
                else:
                    q1_p_cmd = q1_p_ff
                    q2_p_cmd = q2_p_ff
                    self.get_logger().debug(
                        f'T{traj_idx} | iter {i + 1:02d} | '
                        f'[lazo abierto] cmd=({q1_p_cmd:.4f}, {q2_p_cmd:.4f})'
                    )

                vec_msg = Vector3()
                vec_msg.x = q1_p_cmd
                vec_msg.y = q2_p_cmd
                vec_msg.z = 0.0
                self.cmd_pub.publish(vec_msg)

                interrupted = self._stop_event.wait(timeout=PUBLISH_INTERVAL_S)
                if interrupted:
                    traj_interrupted = True
                    break

            self._publish_stop()

            if traj_interrupted:
                set_aborted = True
                break
            else:
                self.get_logger().info(f'  ✓ T{traj_idx} completada.')

        if set_aborted or self._stop_event.is_set():
            self.get_logger().warn(f'⚠ Set {set_id} no completado (abortado o interrumpido).')
        else:
            self.get_logger().info(f'✅ Set {set_id} completado.')

        self._running = False

    # =========================================================================
    # CARGA DE CSVs
    # =========================================================================
    def _load_positions(self, filepath: str) -> list[tuple[float, float]]:
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

    def _load_velocities(self, filepath: str) -> list[tuple[float, float]]:
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
        node._stop_event.set()
        node._idle_timer.cancel()
        node._publish_stop()
        if node._exec_thread is not None:
            node._exec_thread.join(timeout=2.0)
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()