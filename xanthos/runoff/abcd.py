"""
ABCD runoff model.

@author   Chris R. Vernon
@email:   chris.vernon@pnnl.gov
@Project: Xanthos 2.0

License:  BSD 2-Clause, see LICENSE and DISCLAIMER files

Copyright (c) 2017, Battelle Memorial Institute
"""

import numpy as np
from joblib import Parallel, delayed


class ABCD:
    """
    | Hydrology emulator
    |
    | Reference:
    |
    | Liu, Y., Hejazi, M.A., Li, H., Zhang, X., (2017), A Hydrological Emulator for Global
    |    Applications, Geoscientific Model Development Discussions, DOI: 10.5194/gmd-2017-113
    |
    | Martinez, G. F., & Gupta, H. V. (2010). Toward improved identification
    |    of hydrological models: A diagnostic evaluation of the 'abcd' monthly
    |    water balance model for the conterminous United States. Water Resources Research, 46(8).

    @:param prm     Object containing calibrated data
    @:param hist    Object containing Watch data
                    hist.
    @:return a      Float from 0-1
    @:return b      Float from 100-1000 mm
    @:return c      Float from 0-1
    @:return d      Float from 0-1
    @:return m      Float from 0-1
    """

    def __init__(self, pars, pet, precip, tmin, basin_ids, process_steps, spinup_steps, method='dist'):

        # set processing method and steps
        self.method = method
        self.steps = process_steps
        self.spinup_steps = spinup_steps

        self.basin_ids = basin_ids

        # are we running with the snow component?
        self.nosnow = tmin is None

        # assign attributes
        self.a = pars[:, 0]
        self.b = pars[:, 1] * 1000  # note this makes an unnecessary copy
        self.c = pars[:, 2]
        self.d = pars[:, 3]
        self.m = pars[:, 4] if not self.nosnow else 0

        # values [initial runoff, soil moisture storage, groundwater storage
        self.inv = np.array([20, 100, 500])
        self.s0 = self.inv[1]
        self.g0 = self.inv[2]

        # PET as input, transpose to row based
        self.pet = pet.T[0:self.steps, :]

        # precipitation as input
        self.precip = precip.T[0:self.steps, :]

        self.pet0 = self.pet[0:self.spinup_steps, :].copy()
        self.p0 = self.precip[0:self.spinup_steps, :].copy()

        # temperature minimum as input, if provided
        if self.nosnow:
            self.tmin = None
            self.tmin0 = None
        else:
            self.tmin = tmin.T[0:self.steps, :]
            self.tmin0 = self.tmin[0:self.spinup_steps, :].copy()

        # populate param arrays
        self.snm = None
        self.ea = None
        self.re = None
        self.dr = None
        self.base = None
        self.g = None
        self.s = None
        self.w = None
        self.y = None
        self.xs = None
        self.rsim = None
        self.rain = None
        self.snow = None
        self.sn0 = 0
        self.train = 2.5
        self.tsnow = 0.6

    def set_ea(self, p, frac=0.6):
        """
        Set the initial value of actual evapotranspiration (ET) at 60% of precipitation.

        :return: array
        """
        arr = np.zeros_like(p)

        arr[0, :] = p[0, :] * frac

        return arr

    def set_xs(self, p):
        """
        Set the initial value of accumulated snow water at 10% of precipitation.

        :return: array
        """
        arr = np.zeros_like(p)

        arr[0, :] = p[0, :] / 10

        return arr

    def set_rsim(self, p, v):
        """
        Set the initial streamflow.

        :param p:   precipitation
        :param v:   initial runoff

        :return: array
        """
        arr = np.zeros_like(p)

        arr[0, :] = v

        return arr

    def get_rs(self, p, tmin):
        """
        Assign rain and snow arrays.
        """
        # we only need rain array if running without snow
        if self.nosnow:
            self.rain = p
            return

        # construct snow and rain arrays like precip shape
        self.rain = np.zeros_like(p)
        self.snow = np.zeros_like(p)

        # get the indices of each value meeting criteria
        allrain = tmin > self.train
        rainorsnow = (tmin <= self.train) & (tmin >= self.tsnow)
        allsnow = tmin < self.tsnow

        # populate the snow array
        self.snow[rainorsnow] = p[rainorsnow] * (self.train - tmin[rainorsnow]) / (self.train - self.tsnow)

        if allrain.any():
            self.rain[allrain] = p[allrain]
            self.snow[allrain] = 0

        if rainorsnow.any():
            self.rain[rainorsnow] = p[rainorsnow] - self.snow[rainorsnow]

        if allsnow.any():
            self.rain[allsnow] = 0
            self.snow[allsnow] = p[allsnow]

    def abcd_dist(self, i, pet, tmin):
        """
        ABCD Model

        @:param i       Current month
        @:param pet     Potential Evapotranspiration
        @:param tmin    Monthly minimum temperature (if running with snow)
        """

        if not self.nosnow:
            if i == 0:
                self.xs[i, :] = self.sn0 + self.snow[i, :]
            else:
                self.xs[i, :] = self.xs[i - 1, :] + self.snow[i, :]

            # select only snow, intermediate, or only rain for each case
            allrain = tmin[i, :] > self.train
            rainorsnow = (tmin[i, :] <= self.train) & (tmin[i, :] >= self.tsnow)
            allsnow = tmin[i, :] < self.tsnow

            # estimate snowmelt (SNM)
            self.snm[i, allrain] = self.xs[i, allrain] * self.m[allrain]
            self.snm[i, rainorsnow] = (self.xs[i, rainorsnow] * self.m[rainorsnow]) * ((self.train - tmin[i, rainorsnow]) / (self.train - self.tsnow))
            self.snm[i, allsnow] = 0

            # accumulated snow water equivalent
            self.xs[i, :] -= self.snm[i, :]

        # get available water
        if i == 0:
            self.w[i, :] = self.rain[i, :] + self.s0
        else:
            self.w[i, :] = self.rain[i, :] + self.s[i - 1, :] + self.snm[i, :]

        # ET opportunity
        rpt = (self.w[i, :] + self.b)
        pt2 = (2 * self.a)
        self.y[i, :] = rpt / pt2 - np.power(np.power(rpt / pt2, 2) - (self.w[i, :] * self.b / self.a), 0.5)

        # soil water storage
        self.s[i, :] = self.y[i, :] * np.exp(-pet[i, :].real / self.b)

        # get the difference between available water and ET opportunity
        awet = (self.w[i, :] - self.y[i, :])

        # groundwater storage
        if i == 0:
            self.g[i, :] = (self.g0 + self.c * awet) / (1 + self.d)
        else:
            self.g[i, :] = (self.g[i - 1, :] + self.c * awet) / (1 + self.d)

        # populate arrays
        self.ea[i, :] = self.y[i, :] - self.s[i, :]
        self.ea[i, :] = np.maximum(0, self.ea[i, :])
        self.ea[i, :] = np.minimum(pet[i, :].real, self.ea[i, :])
        self.s[i, :] = self.y[i, :] - self.ea[i, :]
        self.re[i, :] = self.c * awet
        self.dr[i, :] = (1 - self.c) * awet
        self.rsim[i, :] = (1 - self.c) * awet + self.d * self.g[i, :]
        self.base[i, :] = self.d * self.g[i, :]

    def init_arrays(self, p, tmin):
        """
        Initialize arrays based on spin-up or simulation run status.
        """
        # construct simulated runoff array
        self.rsim = self.set_rsim(p, self.inv[0])

        self.snm = np.zeros_like(p)
        self.ea = self.set_ea(p)
        self.re = np.zeros_like(p)
        self.dr = np.zeros_like(p)
        self.base = np.zeros_like(p)
        self.g = np.zeros_like(p)
        self.s = np.zeros_like(p)
        self.w = np.zeros_like(p)
        self.y = np.zeros_like(p)
        self.xs = self.set_xs(p)

        # partition snow and rain
        self.get_rs(p, tmin)

    def set_vals(self):
        """
        Reset initial values for runoff [0], soil moisture [1], and groundwater
        storage [2] based on spin-up as the average of the last three Decembers.
        """
        try:
            # get Decembers from end of array (-1=last december, -13=two years ago, -25=three years ago)
            dec_idx = [-1, -13, -25]

        except IndexError:
            print('Spin-up steps must produce at least 10 years spin-up.')
            print('Your spin-up only consist of {} months'.format(self.spinup_steps))
            print('Please reconfigure and try again.')
            raise

        rsim_rollover = self.rsim[dec_idx, :]
        sm_rollover = self.s[dec_idx, :]
        gs_rollover = self.g[dec_idx, :]

        # initial values are set by basin, so here we have to go basin-by-basin
        ro = np.empty(self.basin_ids.shape)
        sm = np.empty(self.basin_ids.shape)
        gs = np.empty(self.basin_ids.shape)

        for i in np.unique(self.basin_ids):
            b_idx = (i == self.basin_ids)
            ro[b_idx] = np.mean(np.nanmean(rsim_rollover[:, b_idx], axis=1))
            sm[b_idx] = np.mean(np.nanmean(sm_rollover[:, b_idx], axis=1))
            gs[b_idx] = np.mean(np.nanmean(gs_rollover[:, b_idx], axis=1))

        self.inv = np.array([ro, sm, gs])
        self.s0 = self.inv[1]
        self.g0 = self.inv[2]

    def spinup(self):
        """
        Run spin-up using initial values.
        """
        # initialize arrays for spinup
        self.init_arrays(self.p0, self.tmin0)

        # run spin-up with initial settings and calibrated a, b, c, d, m params
        for i in range(0, self.spinup_steps, 1):
            self.abcd_dist(i, self.pet0, self.tmin0)

        # reset initial runoff, soil moisture, and groundwater storage values to the average of the last three Decembers
        self.set_vals()

    def simulate(self):
        """
        Run simulation using spin-up values.
        """
        # initialize arrays for simulation from spin-up
        self.init_arrays(self.precip, self.tmin)

        # process with first pass parameters
        for i in range(0, self.steps, 1):
            self.abcd_dist(i, self.pet, self.tmin)

    def emulate(self):
        """
        Run hydrologic emulator.
        """
        # run spin-up
        self.spinup()

        # run simulation
        self.simulate()


def _run_basins(basin_nums, pars_abcdm, basin_ids, pet, precip, tmin, n_months, spinup_steps, method='dist'):
    """
    Run the ABCD model for each basin.

    :param basin_nums:      The numbers of the target basins
    :param n_months:        The number of months to process
    :param spinup_steps:    How many times to tile the historic months by
    :param method:          Either 'dist' for distributed, or 'lump' for lumped processing
    :return                 A NumPy array
    """

    print("\t\tProcessing spin-up and simulation for basins {}".format(basin_nums))

    # get the indices for the selected basins
    basin_indices = np.where(np.isin(basin_ids, basin_nums))

    # pass basin ids to model for setting basin-specific initial values
    _basin_ids = basin_ids[basin_indices]

    # import ABCD parameters for the target basins
    pars_by_cell = pars_abcdm[basin_ids - 1]
    pars = pars_by_cell[basin_indices]

    # extract data for the selected basins only (over all months)
    _pet = pet[basin_indices]
    _precip = precip[basin_indices]

    # tmin is optional, but the snow component of the model will not be used
    # without it
    if tmin is None:
        _tmin = tmin
    else:
        _tmin = tmin[basin_indices]

    # instantiate the model
    he = ABCD(pars, _pet, _precip, _tmin, _basin_ids, n_months, spinup_steps, method=method)

    # run it
    he.emulate()

    # stack outputs
    vals = np.hstack([he.pet.T, he.ea.T, he.rsim.T, he.s.T])

    return vals


def abcd_parallel(num_basins, pars, basin_ids, pet, precip, tmin, n_months, spinup_steps, jobs=-1):
    """
    This model is made to run a basin at a time.  Running them in parallel
    greatly speeds things up. The user can choose to output just the coordinates
    and simulated runoff, or the whole suite of outputs from the ABCD model.

    :param num_basins:          How many basin to run
    :return:                    A list of NumPy arrays
    """
    rslts = _run_basins(range(1, num_basins + 1), pars, basin_ids, pet, precip, tmin, n_months, spinup_steps)

    return rslts


def abcd_execute(n_basins, basin_ids, pet, precip, tmin, calib_file, n_months, spinup_steps, jobs):
    """
    Run the ABCD model.

    :param n_basins:          How many basin to run
    :param basin_ids:         Basin ID Map: 67420 x 1, 235 Basins
    :param pet:               Potential Evapotranspiration for each cell
    :param precip:            Precipitation for each cell
    :param tmin:              Monthly average minimum temperature (optional)
    :param calib_file:        Path to .npy file containing calibrated abcdm parameters
    :param n_months:          The number of months to process
    :param spinup_steps:      How many times to tile the historic months by
    :param jobs:              The number of jobs to use when running basins parallel
                              (-2, all but one core; -1, all cores; 8, 8 cores)

    :return         A NumPy arrays for coordinates (long, lat) and simulated runoff,
                    PET, AET and soil moisture with the shape (grid cells, value per month).
    """
    # read parameters from calibration
    prm = np.load(calib_file)

    # run all basins at once in parallel
    rslts = abcd_parallel(num_basins=n_basins, pars=prm, basin_ids=basin_ids,
                          pet=pet, precip=precip, tmin=tmin, n_months=n_months,
                          spinup_steps=spinup_steps, jobs=jobs)

    # build array to pass to router
    _pet, _aet, _q, _sav = np.split(rslts, 4, axis=1)

    return _pet, _aet, _q, _sav
