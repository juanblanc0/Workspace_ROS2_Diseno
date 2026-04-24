from setuptools import find_packages, setup

package_name = 'package1'

setup(
    name=package_name,
    version='0.0.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='camilo',
    maintainer_email='camilo@todo.todo',
    description='TODO: Package description',
    license='Apache-2.0',
    extras_require={
        'test': [
            'pytest',
        ],
    },
    entry_points={
        'console_scripts': [
            'node1 = package1.node1:main',
            'esp32_node = package1.esp32_serial_node:main',
            'trajectory_sender = package1.trajectory_sender_node:main',
            'firebase_node = package1.firebase_node:main',
        ],
    },
)
