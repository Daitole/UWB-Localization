#! /usr/bin/env python

import rospy
import numpy as np
from ukf.fusion_ukf import FusionUKF
from ukf.datapoint import DataType, DataPoint
from nav_msgs.msg import Odometry
from visualization_msgs.msg import MarkerArray
from gtec_msgs.msg import Ranging
import tf
from tf.transformations import euler_from_quaternion, euler_from_quaternion 
from scipy.optimize import least_squares

class UKFUWBLocalization:
    def __init__(self, uwb_std=1, odometry_std=(1,1,1,1,1,1), accel_std=1, yaw_accel_std=1, alpha=1, beta=0):
        sensor_std = {
            DataType.UWB: {
                'std': [uwb_std],
                'nz': 1
            },
            DataType.ODOMETRY: {
                'std' : odometry_std,
                'nz': 6
            }
        }

        self.ukf = FusionUKF(sensor_std, accel_std, yaw_accel_std, alpha, beta)

        self.anchor_poses = dict()
        self.tag_offset = self.retrieve_tag_offsets({"left_tag":1, "right_tag":0})

        # right: 0
        # left: 1
        # self.tag_offset = {
        #     0:np.array([0, -0.162, 0.184]),
        #     1:np.array([0, 0.162, 0.184])
        # }
        # print(self.tag_offset)

        anchors = '/gtec/toa/anchors'
        toa_ranging = '/gtec/toa/ranging'

        anchors_sub = rospy.Subscriber(anchors, MarkerArray, callback=self.add_anchors)
        ranging_sub = rospy.Subscriber(toa_ranging, Ranging, callback=self.add_ranging)
        odometry = '/odometry/filtered'
        odometry = rospy.Subscriber(odometry, Odometry, callback=self.add_odometry)

        publish_odom = '/jackal/uwb/odom'
        self.estimated_pose = rospy.Publisher(publish_odom, Odometry, queue_size=1)
        self.odom = Odometry()

        self.sensor_data = []

        self.initialized = False
        self.start_translation = np.zeros(2)
        self.start_rotation = 0

    def retrieve_tag_offsets(self, tags, base_link='base_link'):
        transforms = dict() 

        listener = tf.TransformListener()

        rate = rospy.Rate(10.0)

        # right: 0
        # left: 1
        default = {
            0: np.array([0, -0.162, 0.184]),
            1: np.array([0, 0.162, 0.184])
        }

        for tag in tags:
            timeout = 5

            while not rospy.is_shutdown():
                try:
                    (trans,rot) = listener.lookupTransform(base_link, tag, rospy.Time(0))
                    transforms[tags[tag]] = np.array([trans[0], trans[1], trans[2]])
                    break

                except (tf.LookupException, tf.ConnectivityException, tf.ExtrapolationException):
                    if timeout <= 0:
                        transforms[tags[tag]] = default[tags[tag]]
                        break
                    timeout -= 1


                rate.sleep()

        return transforms

    def add_odometry(self, msg):
        t = self.get_time()


        px = msg.pose.pose.position.x
        py = msg.pose.pose.position.y
        pz = msg.pose.pose.position.z

        v = msg.twist.twist.linear.x
        theta = euler_from_quaternion((
            msg.pose.pose.orientation.x,
            msg.pose.pose.orientation.y,
            msg.pose.pose.orientation.z,
            msg.pose.pose.orientation.w
        ))[2]

        theta_yaw = msg.twist.twist.angular.z

        theta_yaw += self.start_rotation
        px += self.start_translation[0]
        py += self.start_translation[1]

        data = DataPoint(DataType.ODOMETRY, np.array([px, py, pz, v, theta, theta_yaw]), t)

        self.sensor_data.append(data)

    def add_anchors(self, msg):
        # type: (MarkerArray) -> None

        for marker in msg.markers:
            self.anchor_poses[marker.id] = np.array([marker.pose.position.x,marker.pose.position.y, marker.pose.position.z]) 

    def get_time(self):
        return rospy.Time.now().to_nsec()

    def add_ranging(self, msg):
        # type: (Ranging) -> None
        t = self.get_time()

        if msg.anchorId in self.anchor_poses:
            anchor_pose = self.anchor_poses[msg.anchorId]
            anchor_distance = msg.range / 1000.

            data = DataPoint(DataType.UWB, anchor_distance, t, extra={
                "anchor": anchor_pose,
                'sensor_offset': self.tag_offset[msg.tagId]
                # 'sensor_offset': None
            })

            self.sensor_data.append(data)

    def intialize(self, x, P):
        t = self.get_time()

        self.ukf.initialize(x, P, t)
        self.initialized = True

    def run(self, initial_P=None):
        if not self.initialized:
            self.initialize_pose(initial_P)

        rate = rospy.Rate(60)

        while not rospy.is_shutdown():

            # start_t = self.get_time()
            for data in self.sensor_data:
                self.ukf.update(data)
            # delta_t = (self.get_time() - start_t) / 1e9
            # print(len(self.sensor_data), delta_t)

            del self.sensor_data[:]


            x, y,z, v, yaw, yaw_rate = self.ukf.x

            self.odom.pose.pose.position.x = x
            self.odom.pose.pose.position.y = y
            self.odom.pose.pose.position.z = z
            self.odom.twist.twist.linear.x = v
            self.odom.twist.twist.angular.z = yaw_rate

            self.estimated_pose.publish(self.odom)

            rate.sleep()

    def func(self, x, d, distances):
        # x[0] = left_x
        # x[1] = left_y
        # x[2] = right_x
        # x[3] = right_y

        x1, y1, x2, y2 = x

        residuals = [
            (x1 - x2) ** 2 + (y1 - y2) ** 2 - d ** 2,
        ]

        for distance in distances:
            anchor = distance['anchor']
            tag = distance['tag']
            distance = distance['dist']

            z = tag[2]

            if np.all(tag == self.tag_offset[1]):
                x = x1
                y = y1
            else:
                x = x2
                y = y2

            residuals.append((x - anchor[0]) ** 2 + (y - anchor[1]) ** 2 + (z - anchor[2]) ** 2 - distance ** 2)

        return residuals

    def initialize_pose(self, initial_P=None):

        delay = 1 #s
        rate = rospy.Rate(delay)

        d = np.linalg.norm(self.tag_offset[0] - self.tag_offset[1])

        while not rospy.is_shutdown() and not self.initialized:
            rate.sleep()

            uwb = []

            for s in self.sensor_data:
                if s.data_type == DataType.UWB:
                    uwb.append({
                        'anchor': s.extra['anchor'],
                        'tag': s.extra['sensor_offset'],
                        'dist': s.measurement_data
                    })

            if len(uwb) > 3:
                res = least_squares(self.func, [0,0,0,0], args=(d, uwb))

                # print(res)

                left = res.x[0:2]
                right = res.x[2:4]
                
                center = (left + right) / 2
                v_ab = left - right
                theta = np.arccos(v_ab[1] / np.linalg.norm(v_ab))

                print(center, v_ab, theta, np.degrees(theta))

                del self.sensor_data[:]

                self.start_translation = center
                self.start_rotation = theta

                if initial_P is None:
                    initial_P = np.identity(6)

                self.intialize(np.array([center[0], center[1], 0, 0, theta, 0 ]), initial_P)


if __name__ == "__main__":
    rospy.init_node("ukf_uwb_localization_kalman")

    intial_pose = rospy.wait_for_message('/ground_truth/state', Odometry)
    x, y, z = intial_pose.pose.pose.position.x, intial_pose.pose.pose.position.y, intial_pose.pose.pose.position.z
    v = 0.2
    theta = euler_from_quaternion((
        intial_pose.pose.pose.orientation.x,
        intial_pose.pose.pose.orientation.y,
        intial_pose.pose.pose.orientation.z,
        intial_pose.pose.pose.orientation.w
    ))[2]

    print x, y, v, theta

    p = [1.0001, 11.0, 14.0001, 20.9001, 1.0001, 0.0001, 0.0001, 3.9001, 4.9001, 1.0, 0, 0.0001, 0.0001, 0.0001, 2.0001, 0.0001, 0.0001]


    loc = UKFUWBLocalization(p[0], p[1:7], accel_std=p[7], yaw_accel_std=p[8], alpha=p[9], beta=p[10])
    # loc.intialize(np.array([x, y, z, v, theta ]),
        # np.identity(6))

    loc.run()

    rospy.spin()
