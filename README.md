# Inicializar nodo comunicacion UART (Motores)

colcon build

source install/setup.bash

ros2 run package1 esp32_node

# Inicializar nodo comunicacion UART (Sensores)

colcon build

source install/setup.bash

ros2 run package1 sensors_node

# Inicializar nodo para planeacion de trayectorias

colcon build

source install/setup.bash

ros2 run package1 trajectory_sender

# Inicializar nodo para comunicacion con Firebase

colcon build

source install/setup.bash

ros2 run package1 firebase_node

# Inicializar nodo para toma de capturas

colcon build

source install/setup.bash

ros2 run package1 camera_node

# Inicializar nodo para clasificacion

colcon build

source install/setup.bash

ros2 run package1 classifier_node

# Comando para enviar trayectoria

ros2 topic pub --once /trajectory_set std_msgs/msg/Int32 "{data: 1}"
