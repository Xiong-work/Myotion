import numpy as np
import scipy.signal as sig
import scipy.fft as sif
import scipy.stats as sst
import pandas as pd
import re
from .xml import *
from multimethod import multimethod

"""
1. max/min/med/std/var/rms/peak-to-peak-distance
2. filter
3. remove-dc offset
4. full-wave rectification
5. interpolation
6. Normalization
7. Regularity/Entropy:  pattern matching, check complexity of waveform
8. on/off detection:    threhold detection
9. co-contraction index:  integration ratio between two waveform
10. zero-crossing:   count zero values


all returned vector remain same dimension
"""


class timeSeriesTable:
    """
    expect input to be a len(labels) x N matrix
    input can be None, but fs and labels must be defined
    """

    def __init__(self, fs, labels, input=None):
        self.data = {}
        self.name = ""

        if len(labels) == 0:
            raise ValueError("at least one label required!")

        if type(input) is dict:
            self.data = input.copy()
        else:
            for i in range(0, len(labels)):
                if input is None:
                    self.data[labels[i]] = np.array([])
                else:
                    self.data[labels[i]] = np.array(input[i].copy())

        self.metadata = {
            "fs": fs,
            "ts": 1.0 / fs,
            "labels": labels.copy(),
            "n": len(self.data[labels[0]]),
            "time": len(self.data[labels[0]]) / fs,
        }

        self.iter = 0

    # accessor of object
    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        # check dimension
        if self.n != 0 and len(value) != self.n:
            raise ValueError("all rows need to have same dimension!")
        if key not in self.labels:
            self.__missing__(key)
            # update time if first row added
            if self.n == 0:
                self.time = len(value) / self.fs
        self.data[key] = value

    def __delitem__(self, key):
        if key not in self.labels:
            return
        del self.data[key]
        self.labels.remove(key)

    def __missing__(self, key):
        self.labels.append(key)
        self.data[key] = np.array([])

    def __getattr__(self, key):
        if key in self.metadata:
            return self.metadata[key]
        elif key == "channels":
            return self.metadata["labels"]

    # iterator
    # for c in timeSeriesTable:
    #   c[0] = ...
    def __iter__(self):
        self.iter = 0
        return self

    def __next__(self):
        if self.iter < self.size():
            return self.data[self.labels[self.iter]]
        else:
            raise StopIteration

    def copy(self):
        return timeSeriesTable(self.fs, self.labels.copy(), self.data.copy())

    # number of points
    def size(self):
        return self.n

    # number of channels
    def chanSize(self):
        return len(self.labels)

    def clear(self):
        for i in range(0, len(self.labels)):
            self.data[self.labels[i]] = np.array([])
        self.n = 0
        self.time = 0

    # name of time series
    def setname(self, name):
        self.name = name

    # rename channel
    def renameChannel(self, old, new):
        if old in self.labels:
            self.data[new] = self.data.pop(old)
            self.labels[self.labels.index(old)] = new

    # remove channel
    def removeChannel(self, chan):
        if chan in self.labels:
            self.data.pop(chan)
            del self.labels[self.labels.index(chan)]

    # check if has channel
    def hasChannel(self, chan):
        if chan in self.labels:
            return True
        else:
            return False

    # convert to pandas using panda series
    def toPandasFrame(self):
        return pd.DataFrame({key: pd.Series(value) for key, value in self.data.items()})

    # get time step in linspace format
    def getLinspace(self):
        return np.linspace(0, self.time, self.n)

    # search channels in regex
    def searchChannel(self, regex):
        to_be_ret = []
        for c in self.labels:
            if re.search(regex, c) is not None:
                to_be_ret.append(c)

        return to_be_ret

    # filter out channels not in regex
    def filterChannel(self, regex):
        to_be_del = []
        for c in self.labels:
            if re.search(regex, c) is None:
                to_be_del.append(c)

        new_labels = []
        for c in self.labels:
            if c in to_be_del:
                del self.data[c]
            else:
                new_labels.append(c)
        self.labels = new_labels

    # =================================== #
    #              time domain            #
    # =================================== #
    @multimethod
    def max(self, key: str):
        return self.data[key].max()

    @multimethod
    def max(self):
        return [self.max(key) for key in self.labels]

    @multimethod
    def min(self, key: str):
        return self.data[key].min()

    @multimethod
    def min(self):
        return [self.min(key) for key in self.labels]

    @multimethod
    def mean(self, key: str):
        return self.data[key].mean()

    @multimethod
    def mean(self):
        return [self.mean(key) for key in self.labels]

    @multimethod
    def median(self, key: str):
        return np.median(self.data[key])

    @multimethod
    def median(self):
        return [self.median(key) for key in self.labels]

    @multimethod
    def std(self, key: str):
        return np.std(self.data[key])

    @multimethod
    def std(self):
        return [self.std(key) for key in self.labels]

    # variance
    @multimethod
    def var(self, key: str):
        return np.var(self.data[key])

    @multimethod
    def var(self):
        return [self.var(key) for key in self.labels]

    @multimethod
    def rms(self, key: str):
        return np.sqrt(np.mean(self.data[key] ** 2))

    @multimethod
    def rms(self):
        return [self.rms(key) for key in self.labels]

    # peak to peak
    @multimethod
    def ptp(self, key: str):
        return np.ptp(self.data[key])

    @multimethod
    def ptp(self):
        return [self.ptp(key) for key in self.labels]

    # remove dc offset
    @multimethod
    def removeDC(self, key: str):
        return self.data[key] - self.mean(key)

    @multimethod
    def removeDC(self):
        return [self.removeDC(key) for key in self.labels]

    @multimethod
    def rectification(self, key: str):
        return np.absolute(self.data[key])

    @multimethod
    def rectification(self):
        return [self.rectification(key) for key in self.labels]

    @multimethod
    def normalization(self, key: str, val):
        if val <= 0:
            raise ValueError("normalization val has be bigger than zero")
        return np.divide(self.data[key], float(val))

    @multimethod
    def normalization(self, val):
        return [self.normalization(key, val) for key in self.labels]

    # intergation along time
    @multimethod
    def trapz(self, key: str):
        return np.trapz(self.data[key])

    @multimethod
    def trapz(self):
        return [self.trapz(key) for key in self.labels]

    @multimethod
    def countZeros(self, key: str):
        return len(self.data[key]) - np.count_nonzero(self.data[key])

    @multimethod
    def countZeros(self):
        return [self.countZeros(key) for key in self.labels]

    @multimethod
    def meanAbsoluate(self, key: str):
        return np.absolute(self.data[key]).mean()

    @multimethod
    def meanAbsoluate(self):
        return [self.meanAbsoluate(key) for key in self.labels]

    @multimethod
    def skew(self, key: str):
        return sst.skew(self.data[key])

    @multimethod
    def skew(self):
        return [self.skew(key) for key in self.labels]

    @multimethod
    def kurtosis(self, key: str):
        return sst.kurtosis(self.data[key])

    @multimethod
    def kurtosis(self):
        return [self.kurtosis(key) for key in self.labels]

    # co-contraction
    def cocontraction(self, key1, key2):
        return np.trapz(self.data[key1]) / np.trapz(self.data[key2])

    def entropy(self, key):
        return

    # threhold detection
    # https://github.com/BMClab/BMC/blob/master/notebooks/DetectOnset.ipynb
    # slow impl
    def threholdDetection(self, key, threhold, n_above=10, n_below=10):
        if n_above < 0 or n_above >= self.n or n_below < 0 or n_below >= self.n:
            raise ValueError(
                "sliding windows has to be a postive value and smaller then total points!"
            )

        activated = []

        above_counter = 0
        below_counter = 0
        isactivated = False
        seg = [0, 0]

        for i in range(0, len(self.data[key])):
            p = self.data[key][i]
            if p >= threhold:  # above
                below_counter = 0  # clear below counter
                if isactivated:
                    continue

                if above_counter < n_above:
                    above_counter += 1
                if above_counter >= n_above:
                    isactivated = True
                    seg[0] = i
            else:  # below
                above_counter = 0
                if not isactivated:
                    continue

                if below_counter < n_below:
                    below_counter += 1
                if below_counter >= n_below:
                    isactivated = False
                    seg[1] = i
                    activated.append(seg)
                    seg = [0, 0]
                    above_counter = 0
                    below_counter = 0

        if isactivated:
            activated.append([seg[0], len(self.data[key])])

        return activated

    # digital butterWorth filter
    def __butterWorth(self, N, Wn, btype):
        return sig.butter(N, Wn, btype, False, "sos", self.fs)

    # return ndarray with filterd data
    def lowpass(self, key, Wn, N=2):
        if Wn <= 0 or Wn >= self.fs / 2:
            raise ValueError("frequency must be 0 < Wn < fs/2")
        # create low pass filter
        sos = self.__butterWorth(N, Wn, "lp")
        return sig.sosfiltfilt(sos, self.data[key])

    # return ndarray with filterd data
    def bandpass(self, key, Wlow, Whigh, N=2):
        if Wlow <= 0 or Wlow >= self.fs / 2:
            raise ValueError("frequency must be 0 < Wn < fs/2")
        if Whigh <= 0 or Whigh >= self.fs / 2:
            raise ValueError("frequency must be 0 < Wn < fs/2")
        # create band pass filter
        sos = self.__butterWorth(N, [Wlow, Whigh], "bp")
        return sig.sosfiltfilt(sos, self.data[key])

    # =================================== #
    #          frequency domain           #
    # =================================== #
    # return (frequency, abs(Intensity))
    # Hann window applied before FFT to reduce spectral leakage.
    @multimethod
    def fft(self, key):
        data = np.array(self.data[key], dtype=float)
        window = np.hanning(len(data))
        return (
            sif.fftfreq(self.n, d=self.ts)[: self.n // 2],
            np.abs(sif.fft(data * window)[: self.n // 2]),
        )

    @multimethod
    def fft(self, key, l_t: float, r_t: float):
        left = min(self.n, int(l_t / self.time * self.n))
        right = min(self.n, int(left + (r_t - l_t) / self.time * self.n))
        segment = np.array(self.data[key][left:right], dtype=float)
        n_seg = len(segment)
        if n_seg == 0:
            return np.array([]), np.array([])
        window = np.hanning(n_seg)
        return (
            sif.fftfreq(n_seg, d=self.ts)[: n_seg // 2],
            np.abs(sif.fft(segment * window)[: n_seg // 2]),
        )

    @multimethod
    def fft_db(self, key):
        x, y = self.fft(key)
        return x, 20 * np.log10(np.maximum(y, 1e-10))

    @multimethod
    def fft_db(self, key, l_t: float, r_t: float):
        x, y = self.fft(key, l_t, r_t)
        return x, 20 * np.log10(np.maximum(y, 1e-10))

    @multimethod
    def meanFreq(self, key: str):
        # https://luscinia.sourceforge.net/page26/page35/page35.html
        freq, val = self.fft(key)
        return np.dot(freq, val) / np.sum(val)

    @multimethod
    def meanFreq(self, key: str, l_t: float, r_t: float):
        freq, val = self.fft(key, l_t, r_t)
        return np.dot(freq, val) / np.sum(val)

    @multimethod
    def meanFreq(self):
        return [self.meanFreq(key) for key in self.labels]

    @multimethod
    def medFreq(self, key: str):
        # https://luscinia.sourceforge.net/page26/page36/page36.html
        freq, val = self.fft(key)
        return freq[
            min(
                len(freq) - 1,
                np.searchsorted(np.cumsum(val), np.sum(val) / 2, side="right"),
            )
        ]

    @multimethod
    def medFreq(self):
        return [self.medFreq(key) for key in self.labels]

    """
    @multimethod
    def spectralCentriod(self, key : str):
        freq, val = self.fft(key)
        return np.dot(freq, val) / np.sum(val)
    """

    # band power in format
    # return: { delta : val0 , theta : val1, alpha : val2 , beta : val3 , gamma : val4 }
    @multimethod
    def BandPower(self, key: str):
        powerrange = {
            "delta": (0.5, 4),
            "theta": (4, 8),
            "alpha": (8, 13),
            "beta": (13, 30),
            "gamma": (30, float("inf")),
        }
        ans = {}
        for name, range in powerrange.items():
            freq, val = self.fft(key)
            l = np.searchsorted(freq, range[0])
            r = np.searchsorted(freq, range[1])
            ans[name] = np.trapz(freq[l:r], val[l:r])
        return ans

    # band power for all channels
    # format [ {result of chan0}, {result of chan1}, ... ]
    @multimethod
    def BandPower(self):
        return [self.BandPower(key) for key in self.labels]

    def loadFile(self, file):
        # load from file
        return 0

    def writeFile(self, file):
        # write to file
        return 0

    """
    <timeSeriesTable>
        <channels_num></channels_num>
        <channels_name> </channels_name>
        <fs> </fs>
        <N> </N>
        <channels>
            <A>  </A>
            ...
        </channels>
    </timeSeriesTable>
    """

    def toXML(self):
        e = xmlElement("timeSeriesTable", {"name": self.name})
        e.addNode("channels_num", xmlString(len(self.labels)))
        e.addNode("channels_name", xmlString(self.labels))
        e.addNode("fs", xmlString(float(self.fs)))
        e.addNode("N", xmlString(self.n))

        c = xmlElement("channels")
        e.addSubTree(c)
        for k in self.labels:
            # c.addNode(k, " ".join(format(x, ".6f") for x in self.data[k]))
            c.addNode(k, self.data[k])
        return e

    @staticmethod
    def fromXML(xml):
        root = xml.find("timeSeriesTable")
        if root == None:
            return None

        e = root.find("channels_num")
        if e == None or e.text == None:
            return None
        chan_num = xmlStringParse(e.text, int)

        e = root.find("channels_name")
        if e == None or e.text == None:
            return None

        chan_name = xmlStringParseList(e.text)
        if len(chan_name) != chan_num:
            return None

        e = root.find("fs")
        if e == None or e.text == None:
            return None
        fs = xmlStringParse(e.text, float)

        e = root.find("N")
        if e == None or e.text == None:
            return None
        N = xmlStringParse(e.text, int)

        e = root.find("channels")
        if e == None:
            return None

        data = []
        for el in e:
            data.append([float(k) for k in xmlStringParseList(el.text)])

        return timeSeriesTable(fs, chan_name, data)
