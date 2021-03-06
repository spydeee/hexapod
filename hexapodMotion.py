#!/usr/bin/env python

from __future__ import division
from Adafruit_PWM_Servo_Driver import PWM
import scipy.optimize
from common import *
import time 
import math
import thread

# some trig functions converted to degrees (not used when performance is needed)
def sin(angle):
	return math.sin(math.radians(angle))

def asin(x):
	return math.degrees(math.asin(x))
	
def cos(angle):
	return math.cos(math.radians(angle))

def acos(x):
	return math.degrees(math.acos(x))
	
def tan(angle):
	return math.tan(math.radians(angle))

	
# mechanical parameters in cm (between rotational points)
coxaFemurLen = 1.7
femurLen = 8.0
tibiaLen = 12.5


# this sets the frequency at which pulses are sent to the servos. 1000/pwmFrequency ms for each cycle
pwmFrequency = 60

# global absolute limits for servo travel. pulseLen/4096 is the ratio of time the pulse is set high in each cycle
minPulseLen = 170
maxPulseLen = 580

# coxa positive movement ccw from top view (0 point with leg perpendicular to body)
# femur positive movement ccw from rearview (0 point parallel to floor)
# tibia positive movement ccw from rearview (0 point when right angle to femur)
# leg 1 is the rear right leg. leg number increasing ccw looking from top

# servo calibration values. order: pulseLen1, pulseLen2, degrees at pulseLen1, degrees at pulseLen2. linear interpolation/extrapolation is used from these calibration values
servoParameters = { 
	"coxa1": [376, 255, 0, 45],
	"coxa2": [353, 255, 0, 40],
	"coxa3": [365, 255, 0, 43],
	"coxa4": [360, 455, 0, 29],
	"coxa5": [401, 475, 0, 25],
	"coxa6": [365, 475, 0, 35],
	"femur1": [369, 575, 0, 75],
	"femur2": [351, 568, 0, 77],
	"femur3": [386, 192, 0, 73],
	"femur4": [392, 580, 0, 67],
	"femur5": [380, 180, 0, 75],
	"femur6": [382, 170, 0, 69],
	"tibia1": [340, 582, 0, 75],
	"tibia2": [340, 180, 0, 75],
	"tibia3": [359, 180, 0, 75],
	"tibia4": [380, 582, 0, 75],
	"tibia5": [347, 582, 0, 75],
	"tibia6": [367, 180, 0, 75]
}

# channel map on the pwm chips. legs 1-3 are on pwmR, 4-6 on pwmL
servoChans = {
	"coxa1": 0,
	"coxa2": 3,
	"coxa3": 11,
	"coxa4": 4,
	"coxa5": 12,
	"coxa6": 15,
	"femur1": 1,
	"femur2": 4,
	"femur3": 12,
	"femur4": 3,
	"femur5": 11,
	"femur6": 14,
	"tibia1": 2,
	"tibia2": 5,
	"tibia3": 13,
	"tibia4": 2,
	"tibia5": 10,
	"tibia6": 13,
}

# calculates the servo pulse length for a desired angle. assumes servo angle is linear wrt the pulseLen
def getPulseLenFromAngle(legSection, angle):
	totalSteps = servoParameters[legSection][1] - servoParameters[legSection][0]
	totalAngle = servoParameters[legSection][3] - servoParameters[legSection][2]
	slope = totalSteps / totalAngle
	intercept = servoParameters[legSection][0] - slope * servoParameters[legSection][2]
	pulseLen = slope * angle + intercept
	return int(round(pulseLen, 0))

class hexapodMotion:
	# when this is enabled, servos will not be moved
	testMode = False
	
	currentServoAngles = {
	}

	# allows or disallows changing of leg angles. this is to prevent femur going down when coxa is repositioning itself after end of powerstroke and to prevent neddless recalculation
	legCommandLock = {
		'1': False,
		'2': False,
		'3': False,
		'4': False,
		'5': False,
		'6': False
	}
	
	# initial parameters (i.e. while standing and default walking position)
	femurStandStartAngle = 20
	tibiaStandStartAngle = -10
	coxaStandStartAngle	= 0
	
	
	# walk parameters
	walkSpeed = 0 # float value from -1 to 1. sign gives direction of walking
	
	walkResolution = 60 # number of steps for one full walk cycle. some of these are technically "skipped" when repositioning for powerstroke
	stepAngle = 360 / walkResolution # degrees in each small step in the cycle
	coxaWalkSweepAngle = 22 # half of the total sweep angle for each leg
	

	# servo takes 0.22s to go 60 degrees @ 6V. self.walkSpeed depends on this value
	servoMaxSpeed = 60/0.35 # deg/sec. the actual servo speed depends on the angle difference it is commanded to move. for now will just pad it
	stepTimeInterval = 0.001 # the minimum interval in seconds between successive walk, rotation etc. commands. a multiplier for this is calculated based on walkSpeed 
	
	
	# current leg walk angles. on initialization of the class these will get generated by generateLegWalkOffsets
	legWalkAngles = {}
	
	# cache for the femur and tibia angle calculations during walking
	tibiaFemurWalkAnglesCache = {}

	def __init__(self):
		addressPwmL = 0x41
		addressPwmR = 0x40
		
		if self.testMode == False:
			# initialise the PWM devices
			self.pwmL = PWM(addressPwmL, debug=False) # left side (view from rear)
			self.pwmL.setPWMFreq(pwmFrequency) # frequency in Hz 
			self.pwmR = PWM(addressPwmR, debug=False) # right side 
			self.pwmR.setPWMFreq(pwmFrequency) # frequency in Hz
			log("PCA9685 modules initialized. pwmL addr: " + hex(addressPwmL) + "    pwmR addr: " + hex(addressPwmR))


		# calculated initial robot parameters
		self.robotHeight = self.calcRobotHeight(self.femurStandStartAngle, self.tibiaStandStartAngle)
		self.stanceWidth = self.calcStanceWidth(self.femurStandStartAngle, self.tibiaStandStartAngle)
		self.tibiaIntersectWidth = self.calcTibiaIntersectWidth(self.femurStandStartAngle, self.tibiaStandStartAngle)
		
		
		# fill the walkvalues with initial defaults
		self.generateLegWalkOffsets(self.legWalkAngles)
		
		# create servo angle cache
		self.createTibiaFemurWalkAnglesCache()
			
		
	# the initial offsets in the walk angle between legs
	def generateLegWalkOffsets(self, legWalkAngles):
		legWalkAngles['1'] = 150
		legWalkAngles['2'] = 210
		legWalkAngles['3'] = 90
		legWalkAngles['4'] = 180
		legWalkAngles['5'] = 120
		legWalkAngles['6'] = 240
		
	def moveServoToAngle(self, legSection, angle):
		servoChan = servoChans[legSection]
		legNum = int(legSection[-1:])
		pulseLen = getPulseLenFromAngle(legSection, angle)
		if 1 <= legNum <= 3:
			pwm = self.pwmR
		else:
			pwm = self.pwmL
		if minPulseLen <= pulseLen <= maxPulseLen:
			#print "servo " + legSection + " told to go to pos: " + str(pulseLen)
			if self.testMode == False: pwm.setPWM(servoChan, 0, pulseLen)
			self.currentServoAngles[legSection] = angle
		else:
			log("servo " + legSection + " told to go to an out of range pos: " + str(pulseLen))
	

	# walk supporting functions
	
	# calculate direction based on walkSpeed value
	def direction(self):
		if self.walkSpeed < 0: return -1
		if self.walkSpeed == 0: return 0
		if self.walkSpeed > 0: return 1
	
	# modify the walking speed based on float input -1 to 1, i.e. from the right (r3) analog stick
	def stepIntervalMultiplier(self):
		maxLegCommandLockTime = 2 * self.coxaWalkSweepAngle / self.servoMaxSpeed
		minTimeInterval = maxLegCommandLockTime / self.walkResolution # the minimum delay between commanded steps, dependent on servo speed
		
		minScale = int(minTimeInterval / self.stepTimeInterval) + 5 # min scale value. don't want to send commands too fast
		maxScale = 80 # max scale value. set so robot has a reasonable minimum speed
		scale = int((1 - math.fabs(self.walkSpeed)) * maxScale)
		if scale > maxScale: scale == maxScale
		if scale <= 0: scale = minScale
		return (scale)

	# vertical distance from ground(tibia tip) to femur/coxa pivot point (robot height)
	def calcRobotHeight(self, femurAngle, tibiaAngle):
		return tibiaLen * cos(femurAngle + tibiaAngle) - femurLen * sin(femurAngle)

	# horizontal distance (on floor) from tibia tip to femur pivot point
	def calcStanceWidth(self, femurAngle, tibiaAngle):
		return femurLen * cos(femurAngle) + tibiaLen * sin(femurAngle + tibiaAngle)
		
	# horizontal distance from femur pivot point to tibia intersect
	def calcTibiaIntersectWidth(self, femurAngle, tibiaAngle):
		return femurLen * cos(femurAngle) + femurLen * sin(femurAngle) * tan(femurAngle + tibiaAngle)

	
	# lookup cache to prevent needless recalculation of tibia and femur angles (very costly!)
	def putTibiaFemurWalkAnglesInCache(self, coxaAngle, angles):
		if coxaAngle not in self.tibiaFemurWalkAnglesCache: self.tibiaFemurWalkAnglesCache[coxaAngle] = {}
		self.tibiaFemurWalkAnglesCache[coxaAngle] = angles
	
	# perform a tibiaFemurWalkAnglesCache cache lookup
	def getTibiaFemurWalkAnglesInCache(self, coxaAngle):
		if coxaAngle not in self.tibiaFemurWalkAnglesCache: return ["error"]
		else: return self.tibiaFemurWalkAnglesCache[coxaAngle]
		
	# will calculate all angles at once instead of realtime so that it doesn't slow down the speed and repeat calculations needlessly
	def createTibiaFemurWalkAnglesCache(self):
		t = time.time()
		log("Generating tibia and femur walk angles cache...")
		
		# leg loop
		for i in range(1, 7):

			# needs to be run for walkAngle between +90 and +270
			walkAngle = 90
			coxaAngle = 0
			while walkAngle <= 270:
				leg = str(i)
				coxaAngle = self.coxaWalkSweepAngle * sin(walkAngle)
				self.tibiaFemurWalkAngles(leg, coxaAngle)
				walkAngle += self.stepAngle
		

		#print self.tibiaFemurWalkAnglesCache
		log("Generated tibia and femur walk angles cache in " + str(time.time() - t) + " seconds")
		
	# when coxa angle changes during walking, these are the femur and tibia angles to maintain constant robot height and position of tibia tip on floor without slipping
	def tibiaFemurWalkAngles(self, leg, coxaAngle):
		# because only legs 2 and 5 have tibias in line with the femur pivot, these offsets are needed to "fudge" the math for the other legs which are offset...will adjust later
		amountOffset = 20
		coxaAngleOffsets = {
			'1': -amountOffset,
			'2': 0,
			'3': amountOffset,
			'4': amountOffset,
			'5': 0,
			'6': -amountOffset
		}
		coxaAngle += coxaAngleOffsets[leg]

		
		# check if walkAngle already in cache
		cacheResult = self.getTibiaFemurWalkAnglesInCache(coxaAngle)
		if cacheResult[0] != "error":
			return cacheResult
		
		if self.walkSpeed != 0: log("tibiaFemurWalkAngles being calculated for leg: " + leg + " and coxaangle: " + str(coxaAngle))
		
		
		# constraints: robotHeight needs to remain constant and tibia tip at a fixed point on floor for fluid forward movement
		d = self.stanceWidth / cos(coxaAngle) # the length needed so the tibia tip is along the line coinciding with the other tibia tips (parallel to movement direction) so the legs don't slip on the ground
		
		# the exact IK equation. x = femurAngle
		def f(x):
			eqns = (self.robotHeight + femurLen * math.sin(x)) ** 2 + (d - femurLen * math.cos(x)) ** 2 - tibiaLen ** 2
			return eqns
		
		# approximate the femurAngle
		femurAngle = scipy.optimize.newton(f, math.radians(self.femurStandStartAngle)) # make the initial guess the standing start angle
		
		# calculate exactly the tibiaAngle from the approximate femurAngle
		tibiaAngle = math.acos( (self.robotHeight + femurLen * math.sin(femurAngle)) / tibiaLen ) - femurAngle
		
		# convert to degrees
		femurAngle = math.degrees(femurAngle)
		tibiaAngle = math.degrees(tibiaAngle)
		
		# put in cache so don't have to recalculate
		self.putTibiaFemurWalkAnglesInCache(coxaAngle, [femurAngle, tibiaAngle])
		
		#print str(d) + " " + str(femurAngle) + " " + str(tibiaAngle)
		return [femurAngle, tibiaAngle]
			
	# calculate the commanded servo angles
	def walkServoAngles(self, leg):
		# modify the walkAngles
		self.legWalkAngles[leg] = self.legWalkAngles[leg] + (self.direction() * self.stepAngle)
	
		# var to hold the coxa, femur and tibia angles
		angles = {}		
		
		# make sure walkAngle is always between 0-360
		self.legWalkAngles[leg] = self.legWalkAngles[leg] % 360
			
		# supporting vars
		if self.legWalkAngles[leg] >= 0: m = 1
		else: m = -1
		
		# if not in the powerstroke, reposition the leg so it is
		if not ((m * 90) <= self.legWalkAngles[leg] <= (m * 270)):
			self.legCommandLock[leg] = True # flag the leg as locked
			
			# on execution of legTimedUnlock the leg will unlock and reposition to floor
			sleepTime = 2 * self.coxaWalkSweepAngle / self.servoMaxSpeed
			thread.start_new_thread(self.legTimedUnlock, (leg, sleepTime,))
			self.legWalkAngles[leg] = self.legWalkAngles[leg] + self.direction() * 180

		# make sure walkAngle is always between 0-360
		self.legWalkAngles[leg] = self.legWalkAngles[leg] % 360	
			
		# calculate the angles
		angles["coxa"] = self.coxaWalkSweepAngle * sin(self.legWalkAngles[leg])
		ftAngles = self.tibiaFemurWalkAngles(leg, angles["coxa"])
		angles["femur"] = ftAngles[0]
		angles["tibia"] = ftAngles[1]
			
		if self.legCommandLock[leg] == True:
			angles["femur"] = self.femurStandStartAngle + 20
		
		# debugging
		if leg == "5" and self.direction() != 0 and self.testMode == True:
			if self.direction() == 1: print "walking forward"
			elif self.direction() == -1: print "walking backward"
			print str(time.time()) + ": leg" + leg +  " walkangles: " + str(self.legWalkAngles[leg]) + " " + str(angles)
		
		return angles
	
	# lock leg angles from changing
	def legTimedUnlock(self, leg, sleepTime):
		# sleep to allow servo to get to beginning of powerstroke position
		#print "sleeping for" + str(sleepTime)
		time.sleep(sleepTime)
		
		# unlock leg and execute another step so leg will move down to floor
		self.legCommandLock[leg] = False
		self.walkServoAngles(leg)
	
	# stand function
	def stand(self):
		for i in range(1, 7):
			self.moveServoToAngle("coxa" + str(i), self.coxaStandStartAngle)
			self.moveServoToAngle("femur" + str(i), self.femurStandStartAngle + 20)
		 
		time.sleep(0.4)
		for i in range(1, 7):
			self.moveServoToAngle("tibia" + str(i), self.tibiaStandStartAngle)
		
		time.sleep(0.4) 
		for i in range(1, 7):
			self.moveServoToAngle("femur" + str(i), self.femurStandStartAngle)
		time.sleep(0.4)
		
	# walk function
	def walk(self):
		if self.walkSpeed == 0:
			time.sleep(0.1)
			return
		for i in range(1, 7): # leg loop
			leg = str(i)
			
			# call only if leg is not locked
			if self.legCommandLock[leg] == False:				
				# get the angles
				angles = self.walkServoAngles(leg)

				# move the servos
				self.moveServoToAngle("coxa" + leg, angles["coxa"])
				self.moveServoToAngle("femur" + leg, angles["femur"])
				self.moveServoToAngle("tibia" + leg, angles["tibia"])

		if self.testMode == True: print ""
		time.sleep(self.stepTimeInterval * self.stepIntervalMultiplier())
		
	# temporary functions
	def __moveServoToPos(self, legSection, pulseLen):
		servoChan = servoChans[legSection]
		legNum = int(legSection[-1:])
		if 1 <= legNum <= 3:
			pwm = self.pwmR
		else:
			pwm = self.pwmL
		if minPulseLen <= pulseLen <= maxPulseLen:
			pwm.setPWM(servoChan, 0, pulseLen)
	def __testServoOffsets(self):
		self.__moveServoToPos("tibia6", 367)
		#self.pwmR.setPWM(12, 0, 580)
		#self.pwmL.setPWM(3, 0, 170)
		
		for i in range(1, 7):
			self.moveServoToAngle("coxa" + str(i), 0)
			self.moveServoToAngle("femur" + str(i), 50)
			#self.moveServoToAngle("tibia" + str(i), 45)					

		