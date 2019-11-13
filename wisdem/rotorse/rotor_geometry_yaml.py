from __future__ import print_function
import os, sys, copy, time, warnings
import operator

from ruamel_yaml import YAML
from ruamel_yaml.comments import CommentedMap, CommentedSeq

from scipy.interpolate import PchipInterpolator, Akima1DInterpolator, interp1d, RectBivariateSpline
import numpy as np
import jsonschema as json

from wisdem.ccblade.ccblade_component import CCBladeGeometry
from wisdem.ccblade import CCAirfoil
from wisdem.airfoilprep.airfoilprep import Airfoil, Polar

from wisdem.rotorse.precomp import Profile, Orthotropic2DMaterial, CompositeSection, _precomp, PreCompWriter
from wisdem.rotorse.geometry_tools.geometry import AirfoilShape, Curve

# import matplotlib.pyplot as plt
from scipy.ndimage import gaussian_filter

def autoscale_y(ax,margin=0.1):
    """This function rescales the y-axis based on the data that is visible given the current xlim of the axis.
    ax -- a matplotlib axes object
    margin -- the fraction of the total height of the y-data to pad the upper and lower ylims"""

    import numpy as np

    def get_bottom_top(line):
        xd = line.get_xdata()
        yd = line.get_ydata()
        lo,hi = ax.get_xlim()
        y_displayed = yd[((xd>lo) & (xd<hi))]
        h = np.max(y_displayed) - np.min(y_displayed)
        bot = np.min(y_displayed)-margin*h
        top = np.max(y_displayed)+margin*h
        return bot,top

    lines = ax.get_lines()
    bot,top = np.inf, -np.inf

    for line in lines:
        new_bot, new_top = get_bottom_top(line)
        if new_bot < bot: bot = new_bot
        if new_top > top: top = new_top

    ax.set_ylim(bot,top)


def remap2grid(x_ref, y_ref, x, spline=PchipInterpolator):


    try:
        spline_y = spline(x_ref, y_ref)
    except:
        x_ref = np.flip(x_ref, axis=0)
        y_ref = np.flip(y_ref, axis=0)
        spline_y = spline(x_ref, y_ref)

    # error handling for x[-1] - x_ref[-1] > 0 and x[-1]~x_ref[-1]
    try:
        _ = iter(x)
        if x[-1]>max(x_ref) and np.isclose(x[-1], x_ref[-1]):
            x[-1]=x_ref[-1]
    except:
        if np.isclose(x, 0.):
            x = 0.
        if x>max(x_ref) and np.isclose(x, x_ref[-1]):
            x=x_ref[-1]

    y_out = spline_y(x)

    np.place(y_out, y_out < min(y_ref), min(y_ref))
    np.place(y_out, y_out > max(y_ref), max(y_ref))

    return y_out

def remapAirfoil(x_ref, y_ref, x0):
    # for interpolating airfoil surface
    x = copy.copy(x_ref)
    y = copy.copy(y_ref)
    x_in = copy.copy(x0)

    idx_le = np.argmin(x)
    x[:idx_le] *= -1.

    idx = [ix0 for ix0, dx0 in enumerate(np.diff(x_in)) if dx0 >0][0]
    x_in[:idx] *= -1.

    return remap2grid(x, y, x_in)

def arc_length(x, y, z=[]):
    npts = len(x)
    arc = np.array([0.]*npts)
    if len(z) == len(x):
        for k in range(1, npts):
            arc[k] = arc[k-1] + np.sqrt((x[k] - x[k-1])**2 + (y[k] - y[k-1])**2 + (z[k] - z[k-1])**2)
    else:
        for k in range(1, npts):
            arc[k] = arc[k-1] + np.sqrt((x[k] - x[k-1])**2 + (y[k] - y[k-1])**2)

    return arc


def rotate(xo, yo, xp, yp, angle):
    ## Rotate a point clockwise by a given angle around a given origin.
    # angle *= -1.
    qx = xo + np.cos(angle) * (xp - xo) - np.sin(angle) * (yp - yo)
    qy = yo + np.sin(angle) * (xp - xo) + np.cos(angle) * (yp - yo)
    return qx, qy

def trailing_edge_smoothing(data):
    # correction to trailing edge shape for interpolated airfoils that smooths out unrealistic geometric errors
    # often brought about when transitioning between round, flatback, or sharp trailing edges

    # correct for self cross of TE (rare interpolation error)
    if data[-1,1] < data[0,1]:
        temp = data[0,1]
        data[0,1] = data[-1,1]
        data[-1,1] = temp

    # Find indices on Suction and Pressure side for last 85-95% and 95-100% chordwise
    idx_85_95  = [i_x for i_x, xi in enumerate(data[:,0]) if xi>0.85 and xi < 0.95]
    idx_95_100 = [i_x for i_x, xi in enumerate(data[:,0]) if xi>0.95 and xi < 1.]

    idx_85_95_break = [i_idx for i_idx, d_idx in enumerate(np.diff(idx_85_95)) if d_idx > 1][0]+1
    idx_85_95_SS    = idx_85_95[:idx_85_95_break]
    idx_85_95_PS    = idx_85_95[idx_85_95_break:]

    idx_95_100_break = [i_idx for i_idx, d_idx in enumerate(np.diff(idx_95_100)) if d_idx > 1][0]+1
    idx_95_100_SS    = idx_95_100[:idx_95_100_break]
    idx_95_100_PS    = idx_95_100[idx_95_100_break:]

    # Interpolate the last 5% to the trailing edge
    idx_in_PS = idx_85_95_PS+[-1]
    x_corrected_PS = data[idx_95_100_PS,0]
    y_corrected_PS = remap2grid(data[idx_in_PS,0], data[idx_in_PS,1], x_corrected_PS)

    idx_in_SS = [0]+idx_85_95_SS
    x_corrected_SS = data[idx_95_100_SS,0]
    y_corrected_SS = remap2grid(data[idx_in_SS,0], data[idx_in_SS,1], x_corrected_SS)

    # Overwrite profile with corrected TE
    data[idx_95_100_SS,1] = y_corrected_SS
    data[idx_95_100_PS,1] = y_corrected_PS

    return data

def smoothListGaussian(list, degree=5):
    import numpy
    window = degree*2-1
    weight = numpy.array([1.0]*window)
    weightGauss = []
    for i in range(window):
        i = i-degree+1
        frac = i/float(window)
        gauss = 1/(numpy.exp((4*(frac))**2))
        weightGauss.append(gauss)
    weight = numpy.array(weightGauss)*weight
    smoothed = [0.0]*(len(list)-window)
    for i in range(len(smoothed)):
        smoothed[i] = sum(numpy.array(list[i:i+window])*weight)/sum(weight)
    return smoothed



class ReferenceBlade(object):
    def __init__(self):

        # Validate input file against JSON schema
        self.validate        = True       # (bool) run IEA turbine ontology JSON validation
        self.fname_schema    = ''          # IEA turbine ontology JSON schema file

        # Grid sizes
        self.NINPUT          = 5
        self.NPTS            = 50
        self.NPTS_AfProfile  = 200
        self.NPTS_AfPolar    = 100

        self.r_in            = []          # User definied input grid (must be from 0-1)

        #
        self.analysis_level  = 0           # 0: Precomp, 1: Precomp + write FAST model, 2: FAST/Elastodyn, 3: FAST/Beamdyn)
        self.verbose         = False

        # Precomp analyis
        self.spar_var        = ['']          # name of composite layer for RotorSE spar cap buckling analysis <---- SS first, then PS
        self.te_var          = ''          # name of composite layer for RotorSE trailing edge buckling analysis

        self.xfoil_path      = ''



    def initialize(self, fname_input):
        if self.verbose:
            print('Running initialization: %s' % fname_input)

        # Load input
        self.fname_input = fname_input
        self.wt_ref = self.load_ontology(self.fname_input, validate=self.validate, fname_schema=self.fname_schema)

        t1 = time.time()
        # build blade
        blade = copy.deepcopy(self.wt_ref['components']['blade'])
        af_ref    = {}
        for afi in self.wt_ref['airfoils']:
            if afi['name'] in blade['outer_shape_bem']['airfoil_position']['labels']:
                af_ref[afi['name']] = afi

        # build environment
        blade['assembly'] = copy.deepcopy(self.wt_ref['assembly'])
        blade['environment'] = copy.deepcopy(self.wt_ref['environment'])

        blade = self.set_configuration(blade, self.wt_ref)
        blade = self.remap_composites(blade)
        blade = self.remap_planform(blade, af_ref)
        blade = self.remap_profiles(blade, af_ref, xfoil_path = self.xfoil_path) # run xfoil at given flap angles
        blade = self.remap_polars(blade, af_ref)
        blade = self.calc_composite_bounds(blade)
        blade = self.calc_control_points(blade, self.r_in)

        blade['analysis_level'] = self.analysis_level
        blade['xfoil_path']     = self.xfoil_path

        if self.verbose:
            print('Complete: Geometry Analysis: \t%f s'%(time.time()-t1))

        # Conversion
        if self.analysis_level < 3:
            t2 = time.time()
            blade = self.convert_precomp(blade, self.wt_ref['materials'])
            if self.verbose:
                print('Complete: Precomp Conversion: \t%f s'%(time.time()-t2))
        elif self.analysis_level == 3:
            # sonata/ anba

            # meshing with sonata

            #
            pass

        return blade

    def update(self, blade):

        self.xfoil_path = blade['xfoil_path']

        t1 = time.time()
        blade = self.calc_spanwise_grid(blade)

        blade = self.update_planform(blade)
        blade = self.remap_profiles(blade, blade['AFref'], xfoil_path = self.xfoil_path) # <- added to 'update' in rthick update
        blade = self.remap_polars(blade, blade['AFref']) # <- added to 'update' in rthick update
        blade = self.calc_composite_bounds(blade)

        if self.verbose:
            print('Complete: Geometry Update: \t%f s'%(time.time()-t1))

        # Conversion
        if self.analysis_level < 3:
            t2 = time.time()
            blade = self.convert_precomp(blade)
            if self.verbose:
                print('Complete: Precomp Conversion: \t%f s'%(time.time()-t2))


        return blade

    def load_ontology(self, fname_input, validate=False, fname_schema=''):
        """ Load inputs IEA turbine ontology yaml inputs, optional validation """
        # Read IEA turbine ontology yaml input file
        with open(fname_input, 'r') as myfile:
            t_load = time.time()
            inputs = myfile.read()

        # Validate the turbine input with the IEA turbine ontology schema
        yaml = YAML()
        if validate:
            t_validate = time.time()

            with open(fname_schema, 'r') as myfile:
                schema = myfile.read()
            json.validate(yaml.load(inputs), yaml.load(schema))

            t_validate = time.time()-t_validate
            if self.verbose:
                print('Complete: Schema "%s" validation: \t%f s'%(fname_schema, t_validate))
        else:
            t_validate = 0.

        # return yaml.load(inputs)
        with open(fname_input, 'r') as myfile:
            inputs = myfile.read()

        if self.verbose:
            t_load = time.time() - t_load - t_validate
            print('Complete: Load Input File: \t%f s'%(t_load))

        return yaml.load(inputs)

    def write_ontology(self, fname, blade, wt_out):

        ### this works for dictionaries, but not what ever ordered dictionary nonsenes is coming out of ruamel
        # def format_dict_for_yaml(out):
        # # recursively loop through output dictionary, convert numpy objects to base python
        #     def get_dict(vartree, branch):
        #         return reduce(operator.getitem, branch, vartree)
        #     def loop_dict(vartree_full, vartree, branch):
        #         for var in vartree.keys():
        #             branch_i = copy.copy(branch)
        #             branch_i.append(var)
        #             if type(vartree[var]) in [dict, CommentedMap]:
        #                 loop_dict(vartree_full, vartree[var], branch_i)
        #             else:
        #                 if type(get_dict(vartree_full, branch_i[:-1])[branch_i[-1]]) is np.ndarray:
        #                     get_dict(vartree_full, branch_i[:-1])[branch_i[-1]] = get_dict(vartree_full, branch_i[:-1])[branch_i[-1]].tolist()
        #                 elif type(get_dict(vartree_full, branch_i[:-1])[branch_i[-1]]) is np.float64:
        #                     get_dict(vartree_full, branch_i[:-1])[branch_i[-1]] = float(get_dict(vartree_full, branch_i[:-1])[branch_i[-1]])
        #                 elif type(get_dict(vartree_full, branch_i[:-1])[branch_i[-1]]) in [tuple, list, CommentedSeq]:
        #                     get_dict(vartree_full, branch_i[:-1])[branch_i[-1]] = [loop_dict(obji, obji, []) for obji in get_dict(vartree_full, branch_i[:-1])[branch_i[-1]] if type(obji) in [dict, CommentedMap]]


        #     loop_dict(out, out, [])
        #     return out

        # dict_out = format_dict_for_yaml(dict_out)


        #### Build Output dictionary
        blade_out = copy.deepcopy(blade)

        # Planform
        wt_out['components']['blade']['outer_shape_bem']['airfoil_position']['labels']  = blade_out['outer_shape_bem']['airfoil_position']['labels']
        wt_out['components']['blade']['outer_shape_bem']['airfoil_position']['grid']    = blade_out['outer_shape_bem']['airfoil_position']['grid']

        wt_out['components']['blade']['outer_shape_bem']['chord']['values']             = blade_out['pf']['chord'].tolist()
        wt_out['components']['blade']['outer_shape_bem']['chord']['grid']               = blade_out['pf']['s'].tolist()
        wt_out['components']['blade']['outer_shape_bem']['twist']['values']             = np.radians(blade_out['pf']['theta']).tolist()
        wt_out['components']['blade']['outer_shape_bem']['twist']['grid']               = blade_out['pf']['s'].tolist()
        wt_out['components']['blade']['outer_shape_bem']['pitch_axis']['values']        = blade_out['pf']['p_le'].tolist()
        wt_out['components']['blade']['outer_shape_bem']['pitch_axis']['grid']          = blade_out['pf']['s'].tolist()
        wt_out['components']['blade']['outer_shape_bem']['reference_axis']['x']['values']  = (-1*blade_out['pf']['precurve']).tolist()
        wt_out['components']['blade']['outer_shape_bem']['reference_axis']['x']['grid']    = blade_out['pf']['s'].tolist()
        wt_out['components']['blade']['outer_shape_bem']['reference_axis']['y']['values']  = blade_out['pf']['presweep'].tolist()
        wt_out['components']['blade']['outer_shape_bem']['reference_axis']['y']['grid']    = blade_out['pf']['s'].tolist()
        wt_out['components']['blade']['outer_shape_bem']['reference_axis']['z']['values']  = blade_out['pf']['r'].tolist()
        wt_out['components']['blade']['outer_shape_bem']['reference_axis']['z']['grid']    = blade_out['pf']['s'].tolist()

        # Composite layups
        # st = copy.deepcopy(blade['st'])
        st = blade_out['st']

        # for var in st['reference_axis'].keys():
        #     try:
        #         _ = st['reference_axis'][var].keys()

        #         st['reference_axis'][var]['grid'] = [float(r) for val, r in zip(st['reference_axis'][var]['values'], st['reference_axis'][var]['grid']) if val != None]
        #         st['reference_axis'][var]['values'] = [float(val) for val in st['reference_axis'][var]['values'] if val != None]
        #         reference_axis
        #         if st['reference_axis'][idx_sec][var]['values'] == []:
        #             del st['reference_axis'][var]
        #             continue
        #     except:
        #         pass

        idx_sec_all = list(range(len(st['layers'])))
        for idx_sec in idx_sec_all:
            layer_vars = copy.deepcopy(list(st['layers'][idx_sec].keys()))
            for var in layer_vars:
                try:
                    _ = st['layers'][idx_sec][var].keys()

                    st['layers'][idx_sec][var]['grid'] = [float(r) for val, r in zip(st['layers'][idx_sec][var]['values'], st['layers'][idx_sec][var]['grid']) if val != None]
                    st['layers'][idx_sec][var]['values'] = [float(val) for val in st['layers'][idx_sec][var]['values'] if val != None]

                    if st['layers'][idx_sec][var]['values'] == []:
                        del st['layers'][idx_sec][var]
                        continue
                except:
                    pass

        idx_sec_all = list(range(len(st['webs'])))
        for idx_sec in idx_sec_all:
            web_vars = copy.deepcopy(list(st['webs'][idx_sec].keys()))
            for var in web_vars:
                try:
                    _ = st['webs'][idx_sec][var].keys()
                    st['webs'][idx_sec][var]['grid'] = [float(r) for val, r in zip(st['webs'][idx_sec][var]['values'], st['webs'][idx_sec][var]['grid']) if val != None]
                    st['webs'][idx_sec][var]['values'] = [float(val) for val in st['webs'][idx_sec][var]['values'] if val != None]

                    if st['layers'][idx_sec][var]['values'] == []:
                        del st['layers'][idx_sec][var]
                        continue
                except:
                    pass
        wt_out['components']['blade']['internal_structure_2d_fem'] = st

        wt_out['components']['blade']['internal_structure_2d_fem']['reference_axis']['x']['values']  = (-1*blade_out['pf']['precurve']).tolist()
        wt_out['components']['blade']['internal_structure_2d_fem']['reference_axis']['x']['grid']    = blade_out['pf']['s'].tolist()
        wt_out['components']['blade']['internal_structure_2d_fem']['reference_axis']['y']['values']  = blade_out['pf']['presweep'].tolist()
        wt_out['components']['blade']['internal_structure_2d_fem']['reference_axis']['y']['grid']    = blade_out['pf']['s'].tolist()
        wt_out['components']['blade']['internal_structure_2d_fem']['reference_axis']['z']['values']  = blade_out['pf']['r'].tolist()
        wt_out['components']['blade']['internal_structure_2d_fem']['reference_axis']['z']['grid']    = blade_out['pf']['s'].tolist()


        ## configuration variables
        for var in wt_out['assembly']['control']:
            if type(blade_out['config'][var]) in [np.float, np.float64, np.float32]:
                wt_out['assembly']['control'][var] = float(blade_out['config'][var])
            else:
                wt_out['assembly']['control'][var] = blade_out['config'][var]
        for var in wt_out['assembly']['global']:
            if type(blade_out['config'][var]) in [np.float, np.float64, np.float32]:
                wt_out['assembly']['global'][var] = float(blade_out['config'][var])
            else:
                wt_out['assembly']['global'][var] = blade_out['config'][var]

        # try:
        f = open(fname, "w")
        yaml=YAML()
        yaml.default_flow_style = None
        yaml.width = float("inf")
        yaml.indent(mapping=4, sequence=6, offset=3)
        yaml.dump(wt_out, f)
        # except:
        #     ontology_out_warning = "WARNING! Ontology output write with ruamel.yaml failed.\n Attemping to write with pyyaml.  All file formatting will be lost (comments and dictionary ordering)."
        #     warnings.warn(ontology_out_warning)
        #     import yaml
        #     f = open(fname, "w")
        #     yaml.dump(wt_out, f)

    def calc_spanwise_grid(self, blade):
        ### Spanwise grid
        # Finds the start and end points of all composite layers, which are required points in the new grid
        # Attempts to roughly evenly space points between the required start/end points to output the user specified grid size

        if 'st' in list(blade):
            st = blade['st']
        else:
            st = blade['internal_structure_2d_fem']

        n = self.NPTS
        # Find unique composite start and end points
        r_points = list(set(list(copy.copy(self.r_in)) + list(blade['outer_shape_bem']['airfoil_position']['grid'])))
        # r_points = list(copy.copy(self.r_in))
        for type_sec, idx_sec, sec in zip(['webs']*len(st['webs'])+['layers']*len(st['layers']), list(range(len(st['webs'])))+list(range(len(st['layers']))), st['webs']+st['layers']):
            for var in sec.keys():
                if type(sec[var]) not in [str, bool]:
                    if 'grid' in sec[var].keys():
                        if len(sec[var]['grid']) > 0.:
                            # remove approximate duplicates
                            r0 = sec[var]['grid'][0]
                            r1 = sec[var]['grid'][-1]

                            r0_close = np.isclose(r0,r_points)
                            if len(r0_close)>0 and any(r0_close):
                                st[type_sec][idx_sec][var]['grid'][0] = r_points[np.argmax(r0_close)]
                            else:
                                r_points.append(r0)

                            r1_close = np.isclose(r1,r_points)
                            if any(r1_close):
                                st[type_sec][idx_sec][var]['grid'][-1] = r_points[np.argmax(r1_close)]
                            else:
                                r_points.append(r1)


        # Check for large enough grid size
        r_points = sorted(r_points)
        n_pts = len(r_points)
        if n_pts > n:
            grid_size_warning = "A grid size of %d was specified, but %d unique composite layer start/end points were found.  It is highly recommended to increase the grid size to >= %d to avoid errors or unrealistic results "%(n, n_pts, n_pts)
            warnings.warn(grid_size_warning)

        #######################################
        # Create grid that includes required points, with as equal as possible spacing between them to reach the grid size
        # finds the number of points to fill inbeween and error handling for n_pts > n

        # equal grid spacing size
        dr = np.diff(np.linspace(r_points[0], r_points[-1], num=n)[0:2])[0]

        # Get initial spacing by placing filler points bases on the linspace step size
        fill = np.zeros(n_pts-1)
        dri = np.zeros(n_pts-1)
        for i in range(1, n_pts):
            fill[i-1] += int((r_points[i] - r_points[i-1]) / dr)
            dri[i-1]   = (r_points[i] - r_points[i-1]) / (fill[i-1] + 1.)

        # Correct initial spacing if there are too many or too few points
        n_out = sum(fill)+n_pts
        while int(n_out) != int(n):
            # Too many points, iteratively remove a point from the range with the smallest spacing
            if n_out > n:
                # find range with the smallest step size where fill > 0, if possible
                if sum(fill) > 0:
                    check      = (fill > 0.)
                    subset_idx = np.argmin(dri[check])
                    idx        = np.arange(n_pts-1)[check][subset_idx]
                # If the number of required points is greater than the grid size, remove points with the smallest step size
                ### there is a warning further up if this is going to occur
                else:
                    idx        = np.argmin(dri)
                # remove a point
                fill[idx] += -1.

            # Too few points, iteratively add a point from the range with the largest spacing
            if n_out < n:
                # find range with the largest step size
                idx        = np.argmax(dri)
                # add a point
                fill[idx] += 1.

            dri[idx] = (r_points[idx+1] - r_points[idx]) / (fill[idx] + 1.)
            n_out = sum(fill)+n_pts

        # Build grid as concatenation of linspaces between required points with respective number of filler points
        grid_out = []
        for i in range(1,n_pts):
            if i == n_pts-1:
                grid_out.append(np.linspace(r_points[i-1], r_points[i], int(fill[i-1]+2)))
            else:
                grid_out.append(np.linspace(r_points[i-1], r_points[i], int(fill[i-1]+2))[:-1])

        self.s = np.concatenate(grid_out)

        if 'st' in list(blade):
            blade['st'] = st
        else:
            blade['internal_structure_2d_fem'] = st

        return blade


    def set_configuration(self, blade, wt_ref):

        blade['config'] = {}

        blade['config']['name']  = wt_ref['name']
        for var in wt_ref['assembly']['global']:
            blade['config'][var] = wt_ref['assembly']['global'][var]
        for var in wt_ref['assembly']['control']:
            blade['config'][var] = wt_ref['assembly']['control'][var]

        return blade

    def remap_planform(self, blade, af_ref):

        blade['pf'] = {}

        blade['pf']['s']        = self.s
        blade['pf']['chord']    = remap2grid(blade['outer_shape_bem']['chord']['grid'], blade['outer_shape_bem']['chord']['values'], self.s)
        blade['pf']['theta']    = np.degrees(remap2grid(blade['outer_shape_bem']['twist']['grid'], blade['outer_shape_bem']['twist']['values'], self.s))
        blade['pf']['p_le']     = remap2grid(blade['outer_shape_bem']['pitch_axis']['grid'], blade['outer_shape_bem']['pitch_axis']['values'], self.s)
        blade['pf']['r']        = remap2grid(blade['outer_shape_bem']['reference_axis']['z']['grid'], blade['outer_shape_bem']['reference_axis']['z']['values'], self.s)
        blade['pf']['precurve'] = -1.*remap2grid(blade['outer_shape_bem']['reference_axis']['x']['grid'], blade['outer_shape_bem']['reference_axis']['x']['values'], self.s)
        blade['pf']['presweep'] = remap2grid(blade['outer_shape_bem']['reference_axis']['y']['grid'], blade['outer_shape_bem']['reference_axis']['y']['values'], self.s)

        thk_ref = [af_ref[af]['relative_thickness'] for af in blade['outer_shape_bem']['airfoil_position']['labels']]
        blade['pf']['rthick']   = remap2grid(blade['outer_shape_bem']['airfoil_position']['grid'], thk_ref, self.s)
        # Smooth oscillation caused by interpolation after min thickness is reached
        idx_min = [i for i, thk in enumerate(blade['pf']['rthick']) if thk == min(thk_ref)]
        if len(idx_min) > 0:
            blade['pf']['rthick']   = np.array([min(thk_ref) if i > idx_min[0] else thk for i, thk in enumerate(blade['pf']['rthick'])])

        # plt.plot(blade['outer_shape_bem']['airfoil_position']['grid'], thk_ref, 'o')
        # plt.plot(self.s, blade['pf']['rthick'])
        # plt.plot(self.s, blade['pf']['rthick'], '.')

        return blade

    def remap_profiles(self, blade, AFref, spline=PchipInterpolator, xfoil_path = ''):

        # Option to correct trailing edge for closed to flatback transition
        trailing_edge_correction = True

        # Get airfoil thicknesses in decending order and cooresponding airfoil names
        AFref_thk = [AFref[af]['relative_thickness'] for af in blade['outer_shape_bem']['airfoil_position']['labels']]

        af_thk_dict = {}
        for afi in blade['outer_shape_bem']['airfoil_position']['labels']:
            afi_thk = AFref[afi]['relative_thickness']
            if afi_thk not in af_thk_dict.keys():
                af_thk_dict[afi_thk] = afi

        af_thk = sorted(af_thk_dict.keys())
        af_labels = [af_thk_dict[afi] for afi in af_thk]

        # Build array of reference airfoil coordinates, remapped
        AFref_n  = len(af_labels)
        AFref_xy = np.zeros((self.NPTS_AfProfile, 2, AFref_n))
        AF_fb = {}

        for afi, af_label in enumerate(af_labels[::-1]):
            points = np.column_stack((AFref[af_label]['coordinates']['x'], AFref[af_label]['coordinates']['y']))

            # check that airfoil points are declared from the TE suction side to TE pressure side
            idx_le = np.argmin(AFref[af_label]['coordinates']['x'])
            if np.mean(AFref[af_label]['coordinates']['y'][:idx_le]) > 0.:
                points = np.flip(points, axis=0)

            if afi == 0:
                af = AirfoilShape(points=points)
                af.redistribute(self.NPTS_AfProfile, even=False, dLE=True)
                s = af.s
                af_points = af.points
            else:
                # print(af_label)
                # print(AFref_xy[:,0,0])
                # print(points[:,0])
                # print(points[:,1])
                # import matplotlib.pyplot as plt
                # plt.plot(points[:,0], points[:,1], '.')
                # plt.plot(points[:,0], points[:,1])
                # plt.show()
                af_points = np.column_stack((AFref_xy[:,0,0], remapAirfoil(points[:,0], points[:,1], AFref_xy[:,0,0])))

            # import matplotlib.pyplot as plt
            # plt.plot(af_points[:,0], af_points[:,1])
            # plt.show()

            if [1,0] not in af_points.tolist():
                af_points[:,0] -= af_points[np.argmin(af_points[:,0]), 0]
            c = max(af_points[:,0])-min(af_points[:,0])
            af_points[:,:] /= c
            AFref_xy[:,:,afi] = af_points

            # if correcting, check for flatbacks
            if trailing_edge_correction:
                if af_points[0,1] == af_points[-1,1]:
                    AF_fb[af_label] = False
                else:
                    AF_fb[af_label] = True


        AFref_xy = np.flip(AFref_xy, axis=2)

        # if trailing_edge_correction:
        #     # closed to flat transition, find spanwise indexes where cylinder/sharp -> flatback
        #     transition = False
        #     for i in range(1,len(blade['outer_shape_bem']['airfoil_position']['labels'])):
        #         afi1 = blade['outer_shape_bem']['airfoil_position']['labels'][i]
        #         afi0 = blade['outer_shape_bem']['airfoil_position']['labels'][i-1]
        #         if AF_fb[afi1] and not AF_fb[afi0]:
        #             transition = True
        #             trans_thk = [AFref[afi0]['relative_thickness'], AFref[afi1]['relative_thickness']]
        #     if transition:
        #         trans_correct_idx = [i_thk for i_thk, thk in enumerate(blade['pf']['rthick']) if thk<trans_thk[0] and thk>trans_thk[1]]
        #     else:
        #         trans_correct_idx = []

        # Spanwise thickness interpolation
        spline = PchipInterpolator
        profile_spline = spline(af_thk, AFref_xy, axis=2)
        blade['profile'] = profile_spline(blade['pf']['rthick'])
        blade['profile_spline'] = profile_spline
        blade['AFref'] = AFref
        blade['flap_profiles']=[]#*self.NPTS

        for i in range(self.NPTS):
            af_le = blade['profile'][np.argmin(blade['profile'][:,0,i]),:,i]
            blade['profile'][:,0,i] -= af_le[0]
            blade['profile'][:,1,i] -= af_le[1]
            c = max(blade['profile'][:,0,i]) - min(blade['profile'][:,0,i])
            blade['profile'][:,:,i] /= c

            # temp = copy.deepcopy(blade['profile'])
            if trailing_edge_correction:
                # if i in trans_correct_idx:
                blade['profile'][:,:,i] = trailing_edge_smoothing(blade['profile'][:,:,i])


            # Use CCAirfoil.af_flap_coords() (which calls Xfoil) to create AF coordinates with flaps at angles specified in yaml input file

            if 'aerodynamic_control' in blade: # Checks if this section is included in yaml file

                blade['flap_profiles'].append({}) # Start appending new dictionary items
                for k in range(len(blade['aerodynamic_control']['te_flaps'])): #for multiple flaps specified in yaml file
                    #if blade['outer_shape_bem']['chord']['grid'][i] >= blade['aerodynamic_control']['te_flaps'][k]['span_start'] and blade['outer_shape_bem']['chord']['grid'][i] <= blade['aerodynamic_control']['te_flaps'][k]['span_end']: # Only create flap geometries where the yaml file specifies there is a flap (Currently going to nearest blade station location)
                    if (blade['pf']['r'][i]/blade['pf']['r'][-1]) >= blade['aerodynamic_control']['te_flaps'][k]['span_start'] and (blade['pf']['r'][i]/blade['pf']['r'][-1]) <= blade['aerodynamic_control']['te_flaps'][k]['span_end']: # Only create flap geometries where the yaml file specifies there is a flap (Currently going to nearest blade station location)
                        blade['flap_profiles'][i]['flap_angles']=[]
                        blade['flap_profiles'][i]['coords']=np.zeros((len(blade['profile'][:,0,0]),len(blade['profile'][0,:,0]),blade['aerodynamic_control']['te_flaps'][k]['num_delta'])) # initialize to zeros
                        flap_angles = np.linspace(blade['aerodynamic_control']['te_flaps'][k]['delta_max_neg'],blade['aerodynamic_control']['te_flaps'][k]['delta_max_pos'],blade['aerodynamic_control']['te_flaps'][k]['num_delta']) # bem:I am not going to force it to include delta=0.  If this is needed, a more complicated way of getting flap deflections to calculate is needed.
                        for ind, fa in enumerate(flap_angles): # For each of the flap angles
                            # NOTE: negative flap angles are deflected to the suction side, i.e. positively along the positive z- (radial) axis
                            af_flap = CCAirfoil(np.array([1,2,3]), np.array([100]), np.zeros(3), np.zeros(3), np.zeros(3), blade['profile'][:,0,i],blade['profile'][:,1,i], "Profile"+str(i)) # bem:I am creating an airfoil name based on index...this structure/naming convention is being assumed in CCAirfoil.runXfoil() via the naming convention used in CCAirfoil.af_flap_coords(). Note that all of the inputs besides profile coordinates and name are just dummy varaiables at this point.
                            af_flap.af_flap_coords(xfoil_path, fa,  blade['aerodynamic_control']['te_flaps'][k]['chord_start'],0.5,200) #bem: the last number is the number of points in the profile.  It is currently being hard coded at 200 but should be changed to make sure it is the same number of points as the other profiles
                            # blade['flap_profiles'][i]['coords'][:,0,ind] = af_flap.af_flap_xcoords # x-coords from xfoil file with flaps
                            # blade['flap_profiles'][i]['coords'][:,1,ind] = af_flap.af_flap_ycoords # y-coords from xfoil file with flaps
                            blade['flap_profiles'][i]['coords'][:,0,ind] = gaussian_filter(af_flap.af_flap_xcoords, sigma=1) # x-coords from xfoil file with flaps and gaussian filter for smoothing
                            blade['flap_profiles'][i]['coords'][:,1,ind] = gaussian_filter(af_flap.af_flap_ycoords, sigma=1) # y-coords from xfoil file with flaps and gaussian filter for smoothing

                            blade['flap_profiles'][i]['flap_angles'].append([])
                            blade['flap_profiles'][i]['flap_angles'][ind] = fa # Putting in flap angles to blade for each profile (can be used for debugging later)
                        # ** The code below will plot the first three flap deflection profiles (in the case where there are only 3 this will correspond to max negative, zero, and max positive deflection cases)
                        # import matplotlib.pyplot as plt
                        # plt.figure
                        # fig, ax = plt.subplots(1, 1, figsize=(8, 5))
                        # # plt.plot(blade['flap_profiles'][i]['coords'][:,0,0], blade['flap_profiles'][i]['coords'][:,1,0], 'r',blade['flap_profiles'][i]['coords'][:,0,1], blade['flap_profiles'][i]['coords'][:,1,1], 'k',blade['flap_profiles'][i]['coords'][:,0,2], blade['flap_profiles'][i]['coords'][:,1,2], 'b')
                        # plt.plot(blade['flap_profiles'][i]['coords'][:, 0, 0],
                        #          blade['flap_profiles'][i]['coords'][:, 1, 0], 'r',
                        #          blade['flap_profiles'][i]['coords'][:, 0, 2],
                        #          blade['flap_profiles'][i]['coords'][:, 1, 2], 'b',
                        #          blade['flap_profiles'][i]['coords'][:, 0, 1],
                        #          blade['flap_profiles'][i]['coords'][:, 1, 1], 'k')
                        #
                        # plt.axis('equal')
                        # plt.show()
                        # plt.savefig('temp/airfoil_polars/NACA63-618_flap_profiles.png')



            # import matplotlib.pyplot as plt
            # plt.plot(temp[:,0,i], temp[:,1,i], 'b')
            # plt.plot(blade['profile'][:,0,i], blade['profile'][:,1,i], 'k')
            # plt.axis('equal')
            # plt.title(i)
            # plt.show()

        return blade

    def remap_polars(self, blade, AFref, spline=PchipInterpolator):


        ## Set angle of attack grid for airfoil resampling
        # assume grid for last airfoil is sufficient

        alpha = np.array(AFref[blade['outer_shape_bem']['airfoil_position']['labels'][-1]]['polars'][0]['c_l']['grid'])
        if alpha[0] != np.radians(-180.):
            alpha[0] = np.radians(-180.)
        if alpha[-1] != np.radians(180.):
            alpha[-1] = np.radians(180.)
        # Re    = [AFref[blade['outer_shape_bem']['airfoil_position']['labels'][-1]]['polars'][0]['re']]

        # get reference airfoil polars
        af_ref = []
        for afi in blade['outer_shape_bem']['airfoil_position']['labels']:
            if afi not in af_ref:
                af_ref.append(afi)

        n_af_ref  = len(af_ref)
        n_aoa     = len(alpha)
        n_span    = self.NPTS

        Re   = sorted(list(set(np.concatenate([[polar['re'] for polar in AFref[afi]['polars']] for afi in AFref]))))  # Re vom yaml input files
        # Re   = list(set(np.concatenate([[polar['re'] for polar in AFref[afi]['polars']] for afi in AFref])))  # not sorting in order to only determine airfoil specific polar tables with default Re
        n_Re = len(Re)

        cl_ref = np.zeros((n_aoa, n_af_ref, n_Re))
        cd_ref = np.zeros((n_aoa, n_af_ref, n_Re))
        cm_ref = np.zeros((n_aoa, n_af_ref, n_Re))
        # Re_ref = np.zeros((n_af_ref, n_Re))

        kx = min(len(alpha)-1, 3)

        for i, af in enumerate(af_ref[::-1]):
            # Remap given polars for this airfoil to common angle of attack grid
            n_Re_i = len(AFref[af]['polars'])
            cl_ref_i = np.zeros((n_aoa, n_Re_i))
            cd_ref_i = np.zeros((n_aoa, n_Re_i))
            cm_ref_i = np.zeros((n_aoa, n_Re_i))

            Re_i      = [polar['re'] for polar in AFref[af]['polars']]
            polar_idx = [j for _,j in sorted(zip(Re_i,range(n_Re_i)))]
            Re_i      = sorted(Re_i)
            for j in polar_idx:
                cl_ref_i[:,j] = remap2grid(np.array(AFref[af]['polars'][j]['c_l']['grid']), np.array(AFref[af]['polars'][j]['c_l']['values']), alpha)
                cd_ref_i[:,j] = remap2grid(np.array(AFref[af]['polars'][j]['c_d']['grid']), np.array(AFref[af]['polars'][j]['c_d']['values']), alpha)
                cm_ref_i[:,j] = remap2grid(np.array(AFref[af]['polars'][j]['c_m']['grid']), np.array(AFref[af]['polars'][j]['c_m']['values']), alpha)

            # Dupplicate lowest and highest polar, set equal to very small and very large Re, allows 'interpolation' outside of provided range
            cl_ref_i = np.c_[cl_ref_i[:,0], cl_ref_i, cl_ref_i[:,-1]]
            cd_ref_i = np.c_[cd_ref_i[:,0], cd_ref_i, cd_ref_i[:,-1]]
            cm_ref_i = np.c_[cm_ref_i[:,0], cm_ref_i, cm_ref_i[:,-1]]
            Re_i     = np.r_[1.e1, Re_i, 1.e15]

            # interpolate over full Re grid
            ky = min(len(Re_i)-1, 3)
            cl_spline = RectBivariateSpline(alpha, Re_i, cl_ref_i, kx=kx, ky=ky, s=0.1)
            cd_spline = RectBivariateSpline(alpha, Re_i, cd_ref_i, kx=kx, ky=ky, s=0.001)
            cm_spline = RectBivariateSpline(alpha, Re_i, cm_ref_i, kx=kx, ky=ky, s=0.0001)
            for j, re in enumerate(Re):
                cl_ref[:,i,j] = cl_spline.ev(alpha, re)
                cd_ref[:,i,j] = cd_spline.ev(alpha, re)
                cm_ref[:,i,j] = cm_spline.ev(alpha, re)


        # reference airfoil and spanwise thicknesses
        thk_span  = blade['pf']['rthick']
        thk_afref = [AFref[af]['relative_thickness'] for af in af_ref[::-1]]
        # error handling for spanwise thickness greater/less than the max/min airfoil thicknesses
        np.place(thk_span, thk_span>max(thk_afref), max(thk_afref))
        np.place(thk_span, thk_span<min(thk_afref), min(thk_afref))


        n_ctrl = 1
        # interpolate
        if 'aerodynamic_control' in blade:
            for afi in range(n_span):
                if 'coords' in blade['flap_profiles'][afi]:
                    n_ctrl = max(n_ctrl, len(blade['flap_profiles'][afi]['flap_angles']))

        cl = np.zeros((n_aoa, n_span, n_Re, n_ctrl))
        cd = np.zeros((n_aoa, n_span, n_Re, n_ctrl))
        cm = np.zeros((n_aoa, n_span, n_Re, n_ctrl))
        fa_control = np.zeros((n_span, n_Re, n_ctrl))
        Re_loc = np.zeros((n_span, n_Re, n_ctrl))
        Ma_loc = np.zeros((n_span, n_Re, n_ctrl))
        for j in range(n_Re):
            spline_cl = spline(thk_afref, cl_ref[:,:,j], axis=1)
            spline_cd = spline(thk_afref, cd_ref[:,:,j], axis=1)
            spline_cm = spline(thk_afref, cm_ref[:,:,j], axis=1)
            cl[:,:,j,0] = spline_cl(thk_span)
            cd[:,:,j,0] = spline_cd(thk_span)
            cm[:,:,j,0] = spline_cm(thk_span)






        from wisdem.ccblade.Polar import Polar
        import csv  # for exporting airfoil polar tables
        import matplotlib.pyplot as plt

        # ----------------------------------------------------- #
        # Determine airfoil polar tables blade sections #

        #  ToDO: shape of blade['profile'] differs from blade['flap_profiles'] <<< change to same shape
        # only execute when flag_airfoil_polars = True
        flag_airfoil_polars = False  # <<< get through Yaml in the future

        if flag_airfoil_polars == True:
            af_orig_grid = blade['outer_shape_bem']['airfoil_position']['grid']
            af_orig_labels = blade['outer_shape_bem']['airfoil_position']['labels']
            af_orig_chord_grid = blade['outer_shape_bem']['chord']['grid']  # note: different grid than airfoil labels
            af_orig_chord_value = blade['outer_shape_bem']['chord']['values']

            for i_af_orig in range(len(af_orig_grid)):
                if af_orig_labels[i_af_orig] != 'circular':
                    print('Determine airfoil polars:')

                    # check index of chord grid for given airfoil radial station
                    for i_chord_grid in range(len(af_orig_chord_grid)):
                        if af_orig_chord_grid[i_chord_grid] == af_orig_grid[i_af_orig]:
                            c = af_orig_chord_value[i_chord_grid]  # get chord length at current radial station of original airfoil
                            c_index = i_chord_grid

                    #  Get orig coordinates (too many for XFoil)
                    # x_af_orig = self.wt_ref['airfoils'][1]['coordinates']['x']
                    # y_af_orig = self.wt_ref['airfoils'][1]['coordinates']['y']

                    #  Get interpolated coords
                    x_af_orig = blade['profile'][:,0,c_index]
                    y_af_orig = blade['profile'][:,1,c_index]


                    rR = af_orig_grid[i_af_orig]  # non-dimensional blade radial station at cross section
                    R = blade['pf']['r'][-1]  # blade (global) radial length
                    tsr = blade['config']['tsr']  # tip-speed ratio
                    maxTS = blade['assembly']['control']['maxTS']  # max blade-tip speed (m/s) from yaml file
                    KinVisc = blade['environment']['air_data']['KinVisc']  # Kinematic viscosity (m^2/s) from yaml file
                    SpdSound = blade['environment']['air_data']['SpdSound']  # speed of sound (m/s) from yaml file
                    Re_af_orig_loc = c * maxTS * rR / KinVisc
                    Ma_af_orig_loc = maxTS * rR / SpdSound

                    print('Run xfoil for airfoil ' + af_orig_labels[i_af_orig] + ' at span section r/R = ' + str(rR) + ' with Re equal to ' + str(Re_af_orig_loc) + ' and Ma equal to ' + str(Ma_af_orig_loc))
                    if af_orig_labels[i_af_orig] == 'NACA63-618':  # reduce AoAmin for (thinner) airfoil at the blade tip due to convergence reasons in XFoil
                        data = self.runXfoil(x_af_orig, y_af_orig, Re_af_orig_loc, -13.5, 25., 0.5, Ma_af_orig_loc)
                    else:
                        data = self.runXfoil(x_af_orig, y_af_orig, Re_af_orig_loc, -20., 25., 0.5, Ma_af_orig_loc)

                    oldpolar = Polar(Re_af_orig_loc, data[:, 0], data[:, 1], data[:, 2], data[:, 4])  # p[:,0] is alpha, p[:,1] is Cl, p[:,2] is Cd, p[:,4] is Cm

                    polar3d = oldpolar.correction3D(rR, c / R, tsr)  # Apply 3D corrections (made sure to change the r/R, c/R, and tsr values appropriately when calling AFcorrections())
                    cdmax = 1.5
                    polar = polar3d.extrapolate(cdmax)  # Extrapolate polars for alpha between -180 deg and 180 deg

                    cl_interp = np.interp(np.degrees(alpha), polar.alpha, polar.cl)
                    cd_interp = np.interp(np.degrees(alpha), polar.alpha, polar.cd)
                    cm_interp = np.interp(np.degrees(alpha), polar.alpha, polar.cm)

                    # --- PROFILE ---#
                    # write profile (that was input to XFoil; although previously provided in the yaml file)
                    with open('temp/airfoil_polars/' + af_orig_labels[i_af_orig] + '_profile.csv', 'w') as profile_csvfile:
                        profile_csvfile_writer = csv.writer(profile_csvfile, delimiter=',')
                        profile_csvfile_writer.writerow(['x', 'y'])
                        for i in range(len(x_af_orig)):
                            profile_csvfile_writer.writerow([x_af_orig[i], y_af_orig[i]])

                    # plot profile
                    plt.figure(i_af_orig)
                    plt.plot(x_af_orig, y_af_orig, 'k')
                    plt.axis('equal')
                    # plt.show()
                    plt.savefig('temp/airfoil_polars/' + af_orig_labels[i_af_orig] + '_profile.png')
                    plt.close(i_af_orig)

                    # --- CL --- #
                    # write cl
                    with open('temp/airfoil_polars/' + af_orig_labels[i_af_orig] + '_cl.csv', 'w') as cl_csvfile:
                        cl_csvfile_writer = csv.writer(cl_csvfile, delimiter=',')
                        cl_csvfile_writer.writerow(['alpha, deg', 'alpha, rad', 'cl'])
                        for i in range(len(cl_interp)):
                            cl_csvfile_writer.writerow([np.degrees(alpha[i]), alpha[i], cl_interp[i]])

                    # plot cl
                    plt.figure(i_af_orig)
                    fig, ax = plt.subplots(1,1, figsize= (8,5))
                    plt.plot(np.degrees(alpha), cl_interp, 'b')
                    plt.xlim(xmin=-25, xmax=25)
                    plt.grid(True)
                    autoscale_y(ax)
                    plt.xlabel('Angles of attack, deg')
                    plt.ylabel('Lift coefficient')
                    # plt.show()
                    plt.savefig('temp/airfoil_polars/' + af_orig_labels[i_af_orig] + '_cl.png')
                    plt.close(i_af_orig)

                    # write cd
                    with open('temp/airfoil_polars/' + af_orig_labels[i_af_orig] + '_cd.csv', 'w') as cd_csvfile:
                        cd_csvfile_writer = csv.writer(cd_csvfile, delimiter=',')
                        cd_csvfile_writer.writerow(['alpha, deg', 'alpha, rad', 'cd'])
                        for i in range(len(cd_interp)):
                            cd_csvfile_writer.writerow([np.degrees(alpha[i]), alpha[i], cd_interp[i]])

                    # plot cd
                    plt.figure(i_af_orig)
                    fig, ax = plt.subplots(1,1, figsize= (8,5))
                    plt.plot(np.degrees(alpha), cd_interp, 'r')
                    plt.xlim(xmin=-25, xmax=25)
                    plt.grid(True)
                    autoscale_y(ax)
                    plt.xlabel('Angles of attack, deg')
                    plt.ylabel('Drag coefficient')
                    # plt.show()
                    plt.savefig('temp/airfoil_polars/' + af_orig_labels[i_af_orig] + '_cd.png')
                    plt.close(i_af_orig)

                    # write cm
                    with open('temp/airfoil_polars/' + af_orig_labels[i_af_orig] + '_cm.csv', 'w') as cm_csvfile:
                        cm_csvfile_writer = csv.writer(cm_csvfile, delimiter=',')
                        cm_csvfile_writer.writerow(['alpha, deg', 'alpha, rad', 'cm'])
                        for i in range(len(cm_interp)):
                            cm_csvfile_writer.writerow([np.degrees(alpha[i]), alpha[i], cm_interp[i]])

                    # plot cm
                    plt.figure(i_af_orig)
                    fig, ax = plt.subplots(1,1, figsize= (8,5))
                    plt.plot(np.degrees(alpha), cm_interp, 'g')
                    plt.xlim(xmin=-25, xmax=25)
                    plt.grid(True)
                    autoscale_y(ax)
                    plt.xlabel('Angles of attack, deg')
                    plt.ylabel('Torque coefficient')
                    # plt.show()
                    plt.savefig('temp/airfoil_polars/' + af_orig_labels[i_af_orig] + '_cm.png')
                    plt.close(i_af_orig)

                    # write additional information (Re, Ma, r/R)
                    with open('temp/airfoil_polars/' + af_orig_labels[i_af_orig] + '_add_info.csv', 'w') as csvfile:
                        csvfile_writer = csv.writer(csvfile, delimiter=',')
                        csvfile_writer.writerow(['Re', 'Ma', 'r/R'])
                        csvfile_writer.writerow([Re_af_orig_loc, Ma_af_orig_loc, rR])


        # ------------------------------------------------------------ #
        # Determine airfoil polar tables for blade sections with flaps #

        if 'aerodynamic_control' in blade:
            for afi in range(n_span): # iterate number of radial stations for various airfoil tables
                if 'coords' in blade['flap_profiles'][afi]: # check if 'coords' is an element of 'flap_profiles', i.e. if we have various flap angles
                    for j in range(n_Re): # ToDo incorporade variable Re capability
                        for ind in range(n_ctrl):
                            #fa = blade['flap_profiles'][afi]['flap_angles'][ind] # value of respective flap angle
                            fa_control[afi,j,ind] = blade['flap_profiles'][afi]['flap_angles'][ind] # flap angle vector of distributed aerodynamics control
                            # eta = (blade['pf']['r'][afi]/blade['pf']['r'][-1])
                            # eta = blade['outer_shape_bem']['chord']['grid'][afi]
                            c   = blade['pf']['chord'][afi]  # blade chord length at cross section
                            R   = blade['pf']['r'][-1]  # blade (global) radial length
                            rR  = (blade['pf']['r'][afi]/blade['pf']['r'][-1])  # non-dimensional blade radial station at cross section
                            tsr = blade['config']['tsr']  # tip-speed ratio
                            maxTS = blade['assembly']['control']['maxTS']  # max blade-tip speed (m/s) from yaml file
                            KinVisc = blade['environment']['air_data']['KinVisc']  # Kinematic viscosity (m^2/s) from yaml file
                            SpdSound = blade['environment']['air_data']['SpdSound'] # speed of sound (m/s) from yaml file
                            Re_loc[afi,j,ind] = c*maxTS*rR/KinVisc
                            Ma_loc[afi,j,ind] = maxTS * rR / SpdSound

                            print('Run xfoil for span section at r/R = ' + str(rR) + ' with ' + str(fa_control[afi,j,ind]) + ' deg flap deflection angle; Re equal to ' + str(Re_loc[afi,j,ind]) + '; Ma equal to ' + str(Ma_loc[afi,j,ind]))
                            if  rR > 0.88:  # reduce AoAmin for (thinner) airfoil at the blade tip due to convergence reasons in XFoil
                                data = self.runXfoil(blade['flap_profiles'][afi]['coords'][:, 0, ind],blade['flap_profiles'][afi]['coords'][:, 1, ind],Re_loc[afi, j, ind], -13.5, 25., 0.5, Ma_loc[afi, j, ind])
                            else:  # normal case
                                data = self.runXfoil(blade['flap_profiles'][afi]['coords'][:, 0, ind],blade['flap_profiles'][afi]['coords'][:, 1, ind],Re_loc[afi, j, ind], -20., 25., 0.5, Ma_loc[afi, j, ind])

                            # data = self.runXfoil(blade['flap_profiles'][afi]['coords'][:,0,ind], blade['flap_profiles'][afi]['coords'][:,1,ind], Re[j])
                            # data[data[:,0].argsort()] # To sort data by increasing aoa
                            # Apply corrections to airfoil polars
                            # oldpolar= Polar(Re[j], data[:,0],data[:,1],data[:,2],data[:,4]) # p[:,0] is alpha, p[:,1] is Cl, p[:,2] is Cd, p[:,4] is Cm
                            oldpolar= Polar(Re_loc[afi,j,ind], data[:,0],data[:,1],data[:,2],data[:,4]) # p[:,0] is alpha, p[:,1] is Cl, p[:,2] is Cd, p[:,4] is Cm

                            polar3d = oldpolar.correction3D(rR,c/R,tsr) # Apply 3D corrections (made sure to change the r/R, c/R, and tsr values appropriately when calling AFcorrections())
                            cdmax   = 1.5
                            polar   = polar3d.extrapolate(cdmax) # Extrapolate polars for alpha between -180 deg and 180 deg

                            cl[:,afi,j,ind] = np.interp(np.degrees(alpha), polar.alpha, polar.cl)
                            cd[:,afi,j,ind] = np.interp(np.degrees(alpha), polar.alpha, polar.cd)
                            cm[:,afi,j,ind] = np.interp(np.degrees(alpha), polar.alpha, polar.cm)

                        # ** The code below will plot the three cl polars
                        # import matplotlib.pyplot as plt
                        # plt.figure
                        # fig, ax = plt.subplots(1, 1, figsize=(8, 5))
                        # plt.plot(np.degrees(alpha), cl[:,afi,j,0],'r', label='$\delta_{flap}$ = -10 deg')  # -10
                        # plt.plot(np.degrees(alpha), cl[:,afi,j,1],'k', label='$\delta_{flap}$ = 0 deg')  # 0
                        # plt.plot(np.degrees(alpha), cl[:,afi,j,2],'b', label='$\delta_{flap}$ = +10 deg')  # +1
                        # plt.xlim(xmin=-15, xmax=15)
                        # plt.ylim(ymin=-1.7, ymax=2.2)
                        # plt.grid(True)
                        # # autoscale_y(ax)
                        # plt.xlabel('Angles of attack, deg')
                        # plt.ylabel('Lift coefficient')
                        # plt.legend(loc='upper left')
                        # plt.show()
                        # plt.savefig('temp/airfoil_polars/NACA63-618_cl_flaps.png')

        alpha_out = np.degrees(alpha)
        if alpha[0] != -180.:
            alpha[0] = -180.
        if alpha[-1] != 180.:
            alpha[-1] = 180.
        for i in range(n_span):
            for j in range(n_Re):
                for k in range(n_ctrl):
                    if cl[0,i,j,k] != cl[-1,i,j,k]:
                        cl[0,i,j,k] = cl[-1,i,j,k]
                    if cd[0,i,j,k] != cd[-1,i,j,k]:
                        cd[0,i,j,k] = cd[-1,i,j,k]
                    if cm[0,i,j,k] != cm[-1,i,j,k]:
                        cm[0,i,j,k] = cm[-1,i,j,k]

        blade['airfoils_cl']  = cl
        blade['airfoils_cd']  = cd
        blade['airfoils_cm']  = cm
        blade['airfoils_aoa'] = alpha_out
        blade['airfoils_Re']  = Re
        #blade['airfoils_Ctrl']  = fa
        blade['airfoils_Ctrl']  = fa_control # use vector of flap angle controls
        blade['Re_loc'] = Re_loc
        blade['Ma_loc'] = Ma_loc



        return blade


    def runXfoil(self, x, y, Re, AoA_min=-9, AoA_max=25, AoA_inc=0.5, Ma = 0.0):
        #This function is used to create and run xfoil simulations for a given set of airfoil coordinates

        # Set initial parameters needed in xfoil
        LoadFlnmAF = "airfoil.txt" # This is a temporary file that will be deleted after it is no longer needed
        numNodes   = 310 # number of panels to use (260...but increases if needed)
        #dist_param = 0.15 # TE/LE panel density ratio (0.15)
        dist_param = 0.12 #This is current value that i am trying to help with convergence (!bem)
        #IterLimit = 100 # Maximum number of iterations to try and get to convergence
        IterLimit = 10 #This decreased IterLimit will speed up analysis (!bem)
        #panelBunch = 1.5 # Panel bunching parameter to bunch near larger changes in profile gradients (1.5)
        panelBunch = 1.6 #This is the value I am currently using to try and improve convergence (!bem)
        #rBunch = 0.15 # Region to LE bunching parameter (used to put additional panels near flap hinge) (0.15)
        rBunch = 0.08 #This is the current value that I am using (!bem)
        XT1 = 0.55 # Defining left boundary of bunching region on top surface (should be before flap)
        #XT2 = 0.85 # Defining right boundary of bunching region on top surface (should be after flap)
        XT2 = 0.9 #This is the value I am currently using (!bem)
        XB1 = 0.55 # Defining left boundary of bunching region on bottom surface (should be before flap)
        #XB2 = 0.85 # Defining right boundary of bunching region on bottom surface (should be after flap)
        XB2 = 0.9 #This is the current value that I am using (!bem)
        saveFlnmPolar = "Polar.txt" # file name of outpur xfoil polar (can be useful to look at during debugging...can also delete at end if you don't want it stored)
        xfoilFlnm  = 'xfoil_input.txt' # Xfoil run script that will be deleted after it is no longer needed
        runFlag = 1 # Flag used in error handling
        dfdn = -0.5 # Change in angle of attack during initialization runs down to AoA_min
        runNum = 0 # Initialized run number
        dfnFlag = -10 # This flag is used to determine if xfoil needs to be re-run if the simulation fails due to convergence issues at low angles of attack

        while numNodes < 480 and runFlag > 0:
            # Cleaning up old files to prevent replacement issues
            if os.path.exists(saveFlnmPolar):
                os.remove(saveFlnmPolar)
            if os.path.exists(xfoilFlnm):
                os.remove(xfoilFlnm)
            if os.path.exists(LoadFlnmAF):
                os.remove(LoadFlnmAF)

            # Writing temporary airfoil coordinate file for use in xfoil
            dat=np.array([x,y])
            np.savetxt(LoadFlnmAF, dat.T, fmt=['%f','%f'])

            # %% Writes the Xfoil run script to read in coordinates, create flap, re-pannel, and create polar
            # Create the airfoil with flap
            fid = open(xfoilFlnm,"w")
            fid.write("PLOP \n G \n\n") # turn off graphics
            fid.write("LOAD \n")
            fid.write( LoadFlnmAF + "\n") # name of .txt file with airfoil coordinates
            # fid.write( self.AFName + "\n") # set name of airfoil (internal to xfoil)
            fid.write("GDES \n") # enter into geometry editing tools in xfoil
            fid.write("UNIT \n") # normalize profile to unit chord
            fid.write("EXEC \n \n") # move buffer airfoil to current airfoil

            # Re-panel with specified number of panes and LE/TE panel density ratio
            fid.write("PPAR\n")
            fid.write("N \n" )
            fid.write(str(numNodes) + "\n")
            fid.write("P \n") # set panel bunching parameter
            fid.write(str(panelBunch) + " \n")
            fid.write("T \n") # set TE/LE panel density ratio
            fid.write( str(dist_param) + "\n")
            fid.write("R \n") # set region panel bunching ratio
            fid.write(str(rBunch) + " \n")
            fid.write("XT \n") # set region panel bunching bounds on top surface
            fid.write(str(XT1) +" \n" + str(XT2) + " \n")
            fid.write("XB \n") # set region panel bunching bounds on bottom surface
            fid.write(str(XB1) +" \n" + str(XB2) + " \n")
            fid.write("\n\n")

            # Set Simulation parameters (Re and max number of iterations)
            fid.write("OPER\n")
            fid.write("VISC \n")
            fid.write( str(Re) + "\n") # this sets Re to value specified in yaml file as an input
            #fid.write( "5000000 \n") # bem: I was having trouble geting convergence for some of the thinner airfoils at the tip for the large Re specified in the yaml, so I am hard coding in Re (5e6 is the highest I was able to get to using these paneling parameters)
            fid.write("MACH\n")
            fid.write(str(Ma)+" \n")
            fid.write("ITER \n")
            fid.write( str(IterLimit) + "\n")

            # Run simulations for range of AoA

            if dfnFlag > 0: # bem: This if statement is for the case when there are issues getting convergence at AoA_min.  It runs a preliminary set of AoA's down to AoA_min (does not save them)
                for ii in range(int((0.0-AoA_min)/AoA_inc+1)):
                    fid.write("ALFA "+ str(0.0-ii*float(AoA_inc)) +"\n")

            fid.write("PACC\n\n\n") #Toggle saving polar on
            #fid.write("ASEQ 0 " + str(AoA_min) + " " + str(dfdn) + "\n") # The preliminary runs are just to get an initialize airfoil solution at min AoA so that the actual runs will not become unstable

            for ii in range(int((AoA_max-AoA_min)/AoA_inc+1)): # bem: run each AoA seperately (makes polar generation more convergence error tolerant)
                fid.write("ALFA "+ str(AoA_min+ii*float(AoA_inc)) +"\n")

            #fid.write("ASEQ " + str(AoA_min) + " " + "16" + " " + str(AoA_inc) + "\n") #run simulations for desired range of AoA using a coarse step size in AoA up to 16 deg
            #fid.write("ASEQ " + "16.5" + " " + str(AoA_max) + " " + "0.1" + "\n") #run simulations for desired range of AoA using a fine AoA increment up to final AoA to help with convergence issues at high Re
            fid.write("PWRT\n") #Toggle saving polar off
            fid.write(saveFlnmPolar + " \n \n")
            fid.write("QUIT \n")
            fid.close()

            # Run the XFoil calling command
            os.system(self.xfoil_path + " < xfoil_input.txt  > NUL") # <<< runs XFoil !
            flap_polar = np.loadtxt(saveFlnmPolar,skiprows=12)


            # Error handling (re-run simulations with more panels if there is not enough data in polars)
            if flap_polar.size < 3: # This case is if there are convergence issues at the lowest angles of attack
                plen = 0
                a0 = 0
                a1 = 0
                dfdn = -0.25 # decrease AoA step size during initialization to try and get convergence in the next run
                dfnFlag = 1 # Set flag to run initialization AoA down to AoA_min
                print('XFOIL convergence issues')
            else:
                plen = len(flap_polar[:,0]) # Number of AoA's in polar
                a0 = flap_polar[-1,0] # Maximum AoA in Polar
                a1 = flap_polar[0,0] # Minimum AoA in Polar
                dfnFlag = -10 # Set flag so that you don't need to run initialization sequence

            if a0 > 19. and plen >= 40 and a1 < -12.5: # The a0 > 19 is to check to make sure polar entered into stall regiem plen >= 40 makes sure there are enough AoA's in polar for interpolation and a1 < -15 makes sure polar contains negative stall.
                runFlag = -10 # No need ro re-run polar
            else:
                numNodes += 50 # Re-run with additional panels
                runNum += 1 # Update run number
                if numNodes > 480:
                    Warning('NO convergence in XFoil achieved!')
                print('Refining paneling to ' + str(numNodes) + ' nodes')

        # Load back in polar data to be saved in instance variables
        #flap_polar = np.loadtxt(saveFlnmPolar,skiprows=12) # (note, we are assuming raw Xfoil polars when skipping the first 12 lines)
        # self.af_flap_polar = flap_polar
        # self.flap_polar_flnm = saveFlnmPolar # Not really needed unless you keep the files and want to load them later

        # Delete Xfoil run script file
        if os.path.exists(xfoilFlnm):
            os.remove(xfoilFlnm)
        if os.path.exists(saveFlnmPolar): # bem: For now leave the files, but eventually we can get rid of them (remove # in front of commands) so that we don't have to store them
            os.remove(saveFlnmPolar)
        if os.path.exists(LoadFlnmAF):
           os.remove(LoadFlnmAF)


        return flap_polar






    def remap_composites(self, blade):
        # Remap composite sections to a common grid
        t = time.time()

        # st = copy.deepcopy(blade_ref['internal_structure_2d_fem'])
        # print('remap_composites copy %f'%(time.time()-t))
        blade = self.calc_spanwise_grid(blade)
        st = blade['internal_structure_2d_fem']

        for var in st['reference_axis']:
            st['reference_axis'][var]['values'] = remap2grid(st['reference_axis'][var]['grid'], st['reference_axis'][var]['values'], self.s).tolist()
            st['reference_axis'][var]['grid'] = self.s.tolist()

        # remap
        for type_sec, idx_sec, sec in zip(['webs']*len(st['webs'])+['layers']*len(st['layers']), list(range(len(st['webs'])))+list(range(len(st['layers']))), st['webs']+st['layers']):
            for var in sec.keys():
                # print(sec['name'], var)
                if type(sec[var]) not in [str, bool]:
                    if 'grid' in sec[var].keys():
                        if len(sec[var]['grid']) > 0.:
                            # if section is only for part of the blade, find start and end of new grid
                            if sec[var]['grid'][0] > 0.:
                                idx_s = np.argmax(np.array(self.s)>=sec[var]['grid'][0])
                            else:
                                idx_s = 0
                            if sec[var]['grid'][-1] < 1.:
                                idx_e = np.argmax(np.array(self.s)>sec[var]['grid'][-1])
                            else:
                                idx_e = -1

                            # interpolate
                            if idx_s != 0 or idx_e !=-1:
                                vals = np.full(self.NPTS, None)
                                vals[idx_s:idx_e] = remap2grid(sec[var]['grid'], sec[var]['values'], self.s[idx_s:idx_e])
                                st[type_sec][idx_sec][var]['values'] = vals.tolist()
                            else:
                                st[type_sec][idx_sec][var]['values'] = remap2grid(sec[var]['grid'], sec[var]['values'], self.s).tolist()
                            st[type_sec][idx_sec][var]['grid'] = self.s

        blade['st'] = st

        return blade


    def calc_composite_bounds(self, blade):

        #######
        def calc_axis_intersection(rotation, offset, p_le_d, side, thk=0.):
            # dimentional analysis that takes a rotation and offset from the pitch axis and calculates the airfoil intersection
            # rotation
            offset_x   = offset*np.cos(rotation) + p_le_d[0]
            offset_y   = offset*np.sin(rotation) + p_le_d[1]

            m_rot      = np.sin(rotation)/np.cos(rotation)       # slope of rotated axis
            plane_rot  = [m_rot, -1*m_rot*p_le_d[0]+ p_le_d[1]]  # coefficients for rotated axis line: a1*x + a0

            m_intersection     = np.sin(rotation+np.pi/2.)/np.cos(rotation+np.pi/2.)   # slope perpendicular to rotated axis
            plane_intersection = [m_intersection, -1*m_intersection*offset_x+offset_y] # coefficients for line perpendicular to rotated axis line at the offset: a1*x + a0

            # intersection between airfoil surface and the line perpendicular to the rotated/offset axis
            y_intersection = np.polyval(plane_intersection, profile_i[:,0])


            try:
                idx_inter      = np.argwhere(np.diff(np.sign(profile_i[:,1] - y_intersection))).flatten() # find closest airfoil surface points to intersection
            except:
                for xi,yi in zip(profile_i[:,0], profile_i[:,1]):
                    print(xi, yi)
                print(rotation, offset, p_le_d, side)
                print('chord', blade['pf']['chord'][i])
                import matplotlib.pyplot as plt
                plt.plot(profile_i[:,0], profile_i[:,1])
                plt.plot(profile_i[:,0], y_intersection)
                plt.show()

                idx_inter      = np.argwhere(np.diff(np.sign(profile_i[:,1] - y_intersection))).flatten() # find closest airfoil surface points to intersection

            midpoint_arc = []
            for sidei in side:
                if sidei.lower() == 'suction':
                    tangent_line = np.polyfit(profile_i[idx_inter[0]:idx_inter[0]+2, 0], profile_i[idx_inter[0]:idx_inter[0]+2, 1], 1)
                elif sidei.lower() == 'pressure':
                    tangent_line = np.polyfit(profile_i[idx_inter[1]:idx_inter[1]+2, 0], profile_i[idx_inter[1]:idx_inter[1]+2, 1], 1)

                midpoint_x = (tangent_line[1]-plane_intersection[1])/(plane_intersection[0]-tangent_line[0])
                midpoint_y = plane_intersection[0]*(tangent_line[1]-plane_intersection[1])/(plane_intersection[0]-tangent_line[0]) + plane_intersection[1]

                # convert to arc position
                if sidei.lower() == 'suction':
                    x_half = profile_i[:idx_le+1,0]
                    arc_half = profile_i_arc[:idx_le+1]
                elif sidei.lower() == 'pressure':
                    x_half = profile_i[idx_le:,0]
                    arc_half = profile_i_arc[idx_le:]

                midpoint_arc.append(remap2grid(x_half, arc_half, midpoint_x, spline=interp1d))

            # if len(idx_inter) == 0:
            # print(blade['pf']['s'][i], blade['pf']['r'][i], blade['pf']['chord'][i], thk)
            # import matplotlib.pyplot as plt
            # plt.plot(profile_i[:,0], profile_i[:,1])
            # plt.axis('equal')
            # ymin, ymax = plt.gca().get_ylim()
            # xmin, xmax = plt.gca().get_xlim()
            # plt.plot(profile_i[:,0], y_intersection)
            # plt.plot(p_le_d[0], p_le_d[1], '.')
            # plt.axis([xmin, xmax, ymin, ymax])
            # plt.show()

            return midpoint_arc
        ########

        # Format profile for interpolation
        profile_d = copy.copy(blade['profile'])
        profile_d[:,0,:] = profile_d[:,0,:] - blade['pf']['p_le'][np.newaxis, :]
        profile_d = np.flip(profile_d*blade['pf']['chord'][np.newaxis, np.newaxis, :], axis=0)

        LE_loc = np.zeros(self.NPTS)
        for i in range(self.NPTS):
            profile_i = copy.copy(profile_d[:,:,i])
            if list(profile_i[-1,:]) != list(profile_i[0,:]):
                TE = np.mean((profile_i[-1,:], profile_i[0,:]), axis=0)
                profile_i = np.row_stack((TE, profile_i, TE))
            idx_le = np.argmin(profile_i[:,0])
            profile_i_arc = arc_length(profile_i[:,0], profile_i[:,1])
            arc_L = profile_i_arc[-1]
            profile_i_arc /= arc_L
            LE_loc[i] = profile_i_arc[idx_le]




        for i in range(self.NPTS):
            s_all = []

            profile_i = copy.copy(profile_d[:,:,i])
            if list(profile_i[-1,:]) != list(profile_i[0,:]):
                TE = np.mean((profile_i[-1,:], profile_i[0,:]), axis=0)
                profile_i = np.row_stack((TE, profile_i, TE))
                # import matplotlib.pyplot as plt
                # plt.plot(profile_i[:,0], profile_i[:,1])
                # plt.plot(TE[0], TE[1], 'o')
                # plt.axis('equal')
                # plt.title(i)
                # plt.show()

            idx_le = np.argmin(profile_i[:,0])

            profile_i_arc = arc_length(profile_i[:,0], profile_i[:,1])
            arc_L = profile_i_arc[-1]
            profile_i_arc /= arc_L

            # loop through composite layups
            for type_sec, idx_sec, sec in zip(['webs']*len(blade['st']['webs'])+['layers']*len(blade['st']['layers']), list(range(len(blade['st']['webs'])))+list(range(len(blade['st']['layers']))), blade['st']['webs']+blade['st']['layers']):
                # for idx_sec, sec in enumerate(blade['st'][type_sec]):

                # initialize chord wise start end points
                if i == 0:
                    # print(sec['name'], blade['st'][type_sec][idx_sec].keys())
                    if all([field not in blade['st'][type_sec][idx_sec].keys() for field in ['midpoint_nd_arc','start_nd_arc','end_nd_arc','rotation','web']]):
                        blade['st'][type_sec][idx_sec]['start_nd_arc'] = {}
                        blade['st'][type_sec][idx_sec]['start_nd_arc']['grid'] = self.s
                        blade['st'][type_sec][idx_sec]['start_nd_arc']['values'] = np.full(self.NPTS, 0.).tolist()
                        blade['st'][type_sec][idx_sec]['end_nd_arc'] = {}
                        blade['st'][type_sec][idx_sec]['end_nd_arc']['grid'] = self.s
                        blade['st'][type_sec][idx_sec]['end_nd_arc']['values'] = np.full(self.NPTS, 1.).tolist()
                    if 'width' in blade['st'][type_sec][idx_sec].keys():
                        blade['st'][type_sec][idx_sec]['start_nd_arc'] = {}
                        blade['st'][type_sec][idx_sec]['start_nd_arc']['grid'] = self.s
                        blade['st'][type_sec][idx_sec]['start_nd_arc']['values'] = np.full(self.NPTS, None).tolist()
                        blade['st'][type_sec][idx_sec]['end_nd_arc'] = {}
                        blade['st'][type_sec][idx_sec]['end_nd_arc']['grid'] = self.s
                        blade['st'][type_sec][idx_sec]['end_nd_arc']['values'] = np.full(self.NPTS, None).tolist()
                    if 'start_nd_arc' not in blade['st'][type_sec][idx_sec].keys():
                        blade['st'][type_sec][idx_sec]['start_nd_arc'] = {}
                        blade['st'][type_sec][idx_sec]['start_nd_arc']['grid'] = self.s
                        blade['st'][type_sec][idx_sec]['start_nd_arc']['values'] = np.full(self.NPTS, None).tolist()
                    if 'end_nd_arc' not in blade['st'][type_sec][idx_sec].keys():
                        blade['st'][type_sec][idx_sec]['end_nd_arc'] = {}
                        blade['st'][type_sec][idx_sec]['end_nd_arc']['grid'] = self.s
                        blade['st'][type_sec][idx_sec]['end_nd_arc']['values'] = np.full(self.NPTS, None).tolist()
                    if 'fiber_orientation' not in blade['st'][type_sec][idx_sec].keys() and type_sec != 'webs':
                        blade['st'][type_sec][idx_sec]['fiber_orientation'] = {}
                        blade['st'][type_sec][idx_sec]['fiber_orientation']['grid'] = self.s
                        blade['st'][type_sec][idx_sec]['fiber_orientation']['values'] = np.zeros(self.NPTS).tolist()
                    if 'rotation' in blade['st'][type_sec][idx_sec].keys():
                        if 'fixed' in blade['st'][type_sec][idx_sec]['rotation'].keys():
                            if blade['st'][type_sec][idx_sec]['rotation']['fixed'] == 'twist':
                                blade['st'][type_sec][idx_sec]['rotation']['grid'] = blade['pf']['s']
                                blade['st'][type_sec][idx_sec]['rotation']['values'] = -np.radians(blade['pf']['theta'])
                            else:
                                warning_invalid_fixed_rotation_reference = 'Invalid fixed reference given for layer = "%s" rotation. Currently supported options: "twist".'%(sec['name'])
                                warnings.warn(warning_invalid_fixed_rotation_reference)


                # If non-dimensional coordinates are given, ignore other methods
                calc_bounds = True
                # if 'values' in blade['st'][type_sec][idx_sec]['start_nd_arc'].keys() and 'values' in blade['st'][type_sec][idx_sec]['end_nd_arc'].keys():
                #     if blade['st'][type_sec][idx_sec]['start_nd_arc']['values'][i] != None and blade['st'][type_sec][idx_sec]['end_nd_arc']['values'][i] != None:
                #         calc_bounds = False

                chord = blade['pf']['chord'][i]

                if calc_bounds:
                    ratio_SCmax = 0.8
                    p_le_i      = blade['pf']['p_le'][i]
                    if 'rotation' in blade['st'][type_sec][idx_sec].keys() and 'width' in blade['st'][type_sec][idx_sec].keys() and 'side' in blade['st'][type_sec][idx_sec].keys() and blade['st'][type_sec][idx_sec]['thickness']['values'][i] not in [None, 0., 0]:

                        # layer midpoint definied with a rotation and offset about the pitch axis
                        rotation   = sec['rotation']['values'][i] # radians
                        width      = sec['width']['values'][i]    # meters
                        p_le_d     = [0., 0.]                     # pitch axis for dimentional profile
                        side       = sec['side']
                        if 'offset_x_pa' in blade['st'][type_sec][idx_sec].keys():
                            offset = sec['offset_x_pa']['values'][i]
                        else:
                            offset = 0.

                        if rotation == None:
                            rotation = 0.
                        if width == None:
                            width = 0.
                        if side == None:
                            side = 0.
                        if offset == None:
                            offset = 0.

                        # # geometry checks
                        if offset + 0.5 * width > ratio_SCmax * chord * (1. - p_le_i) or offset - 0.5 * width < - ratio_SCmax * chord * p_le_i: # hitting TE or LE
                            width_old = copy.deepcopy(width)
                            width     = 2. * min([ratio_SCmax * (chord * p_le_i ) , ratio_SCmax * (chord * (1. - p_le_i))])
                            blade['st'][type_sec][idx_sec]['offset_x_pa']['values'][i] = 0.0
                            blade['st'][type_sec][idx_sec]['width']['values'][i]  = width

                            layer_resize_warning = 'WARNING: Layer "%s" may be too large to fit within chord. "offset_x_pa" changed from %f to 0.0 and "width" changed from %f to %f at R=%f (i=%d)'%(sec['name'], offset, width_old, width, blade['pf']['r'][i], i)
                            print(layer_resize_warning)


                        # if offset < ratio_SCmax * (- chord * p_le_i) or offset > ratio_SCmax * (chord * (1. - p_le_i)):
                            # width_old = copy.deepcopy(width)
                            # width = 2. * min([ratio_SCmax * (chord * p_le_i) , ratio_SCmax * (chord * (1. - p_le_i))])
                            # blade['st'][type_sec][idx_sec]['width']['values'][i] = width
                            # layer_resize_warning = 'WARNING: Layer "%s" may be too large to fit within chord. "width" changed from %f to %f at R=%f (i=%d)'%(sec['name'], width_old, width, blade['pf']['r'][i], i)
                            # warnings.warn(layer_resize_warning)


                        if side.lower() != 'suction' and side.lower() != 'pressure':
                            warning_invalid_side_value = 'Invalid airfoil value give: side = "%s" for layer = "%s" at r[%d] = %f. Must be set to "suction" or "pressure".'%(side, sec['name'], i, blade['pf']['r'][i])
                            warnings.warn(warning_invalid_side_value)

                        midpoint = calc_axis_intersection(rotation, offset, p_le_d, [side], thk=sec['thickness']['values'][i])[0]

                        blade['st'][type_sec][idx_sec]['start_nd_arc']['values'][i] = midpoint-width/arc_L/2.
                        blade['st'][type_sec][idx_sec]['end_nd_arc']['values'][i]   = midpoint+width/arc_L/2.

                    elif 'rotation' in blade['st'][type_sec][idx_sec].keys():
                        # web defined with a rotation and offset about the pitch axis
                        # if 'fixed' in sec['rotation'].keys():
                        #     sec['rotation']['values']
                        rotation   = sec['rotation']['values'][i] # radians
                        p_le_d     = [0., 0.]                     # pitch axis for dimentional profile
                        if 'offset_x_pa' in blade['st'][type_sec][idx_sec].keys():
                            offset = sec['offset_x_pa']['values'][i]
                        else:
                            offset = 0.

                        if rotation == None:
                            rotation = 0
                        if offset == None:
                            offset = 0

                        # geometry checks
                        if offset < ratio_SCmax * (- chord * p_le_i) or offset > ratio_SCmax * (chord * (1. - p_le_i)):
                            offset_old = copy.deepcopy(offset)
                            if offset_old <= 0.:
                                offset = ratio_SCmax * (- chord * p_le_i)
                            else:
                                offset = ratio_SCmax * (chord * (1. - p_le_i))
                            blade['st'][type_sec][idx_sec]['offset_x_pa']['values'][i] = offset
                            layer_resize_warning = 'WARNING: Layer "%s" may be too large to fit within chord. "offset_x_pa" changed from %f to %f at R=%f (i=%d)'%(sec['name'], offset_old, offset, blade['pf']['r'][i], i)
                            print(layer_resize_warning)
                        [blade['st'][type_sec][idx_sec]['start_nd_arc']['values'][i], blade['st'][type_sec][idx_sec]['end_nd_arc']['values'][i]] = sorted(calc_axis_intersection(rotation, offset, p_le_d, ['suction', 'pressure']))

                    elif 'midpoint_nd_arc' in blade['st'][type_sec][idx_sec].keys():
                        # fixed to LE or TE
                        width      = sec['width']['values'][i]    # meters
                        if blade['st'][type_sec][idx_sec]['midpoint_nd_arc']['fixed'].lower() == 'te' or blade['st'][type_sec][idx_sec]['midpoint_nd_arc']['fixed'].lower() == 'TE':
                            midpoint = 1.
                        elif blade['st'][type_sec][idx_sec]['midpoint_nd_arc']['fixed'].lower() == 'le' or blade['st'][type_sec][idx_sec]['midpoint_nd_arc']['fixed'].lower() == 'LE':
                            midpoint = profile_i_arc[idx_le]
                        else:
                            warning_invalid_side_value = 'Invalid fixed midpoint give: midpoint_nd_arc[fixed] = "%s" for layer = "%s" at r[%d] = %f. Must be set to "LE" or "TE".'%(blade['st'][type_sec][idx_sec]['midpoint_nd_arc']['fixed'], sec['name'], i, blade['pf']['r'][i])
                            warnings.warn(warning_invalid_side_value)

                        if width == None:
                            width = 0

                        blade['st'][type_sec][idx_sec]['start_nd_arc']['values'][i] = midpoint-width/arc_L/2.
                        blade['st'][type_sec][idx_sec]['end_nd_arc']['values'][i]   = midpoint+width/arc_L/2.
                        if blade['st'][type_sec][idx_sec]['end_nd_arc']['values'][i] > 1.:
                            blade['st'][type_sec][idx_sec]['end_nd_arc']['values'][i] -= 1.

        # Set any end points that are fixed to other sections, loop through composites again
        for idx_sec, sec in enumerate(blade['st']['layers']):
            if 'fixed' in blade['st']['layers'][idx_sec]['start_nd_arc'].keys():
                blade['st']['layers'][idx_sec]['start_nd_arc']['grid']   = self.s
                target_name  = blade['st']['layers'][idx_sec]['start_nd_arc']['fixed']
                if target_name == 'te' or target_name == 'TE' :
                    blade['st']['layers'][idx_sec]['start_nd_arc']['values'] = np.zeros(self.NPTS)
                elif target_name == 'le' or target_name == 'LE':
                    blade['st']['layers'][idx_sec]['start_nd_arc']['values'] = LE_loc
                else:
                    target_idx   = [i for i, sec in enumerate(blade['st']['layers']) if sec['name']==target_name][0]
                    blade['st']['layers'][idx_sec]['start_nd_arc']['grid']   = blade['st']['layers'][target_idx]['end_nd_arc']['grid'].tolist()
                    blade['st']['layers'][idx_sec]['start_nd_arc']['values'] = blade['st']['layers'][target_idx]['end_nd_arc']['values']


            if 'fixed' in blade['st']['layers'][idx_sec]['end_nd_arc'].keys():
                blade['st']['layers'][idx_sec]['end_nd_arc']['grid']   = self.s
                target_name  = blade['st']['layers'][idx_sec]['end_nd_arc']['fixed']
                if target_name == 'te' or target_name == 'TE':
                    blade['st']['layers'][idx_sec]['end_nd_arc']['values'] = np.ones(self.NPTS)
                elif target_name == 'le' or target_name == 'LE':
                    blade['st']['layers'][idx_sec]['end_nd_arc']['values'] = LE_loc
                else:
                    target_idx   = [i for i, sec in enumerate(blade['st']['layers']) if sec['name']==target_name][0]
                    blade['st']['layers'][idx_sec]['end_nd_arc']['grid']   = blade['st']['layers'][target_idx]['start_nd_arc']['grid'].tolist()
                    blade['st']['layers'][idx_sec]['end_nd_arc']['values'] = blade['st']['layers'][target_idx]['start_nd_arc']['values']



        return blade

    def calc_control_points(self, blade, r_in=[], r_max_chord=0.):

        if 'ctrl_pts' not in blade.keys():
            blade['ctrl_pts'] = {}

        # solve for max chord radius
        if r_max_chord == 0.:
            r_max_chord = blade['pf']['s'][np.argmax(blade['pf']['chord'])]

        # solve for end of cylinder radius by interpolating relative thickness
        idx = max([i for i, thk in enumerate(blade['pf']['rthick']) if thk == 1.])
        if idx > 0:
            r_cylinder  = blade['pf']['s'][idx]
        else:
            cyl_thk_min = 0.98
            idx_s       = np.argmax(blade['pf']['rthick']<1)
            idx_e       = np.argmax(np.isclose(blade['pf']['rthick'], min(blade['pf']['rthick'])))
            r_cylinder  = remap2grid(blade['pf']['rthick'][idx_e:idx_s-2:-1], blade['pf']['s'][idx_e:idx_s-2:-1], cyl_thk_min)

        # Build Control Point Grid, if not provided with key word arg
        if len(r_in)==0:
            # Set control point grid, which is to be updated when chord changes
            r_in = np.hstack([0., r_cylinder, np.linspace(r_max_chord, 1., self.NINPUT-2)])
            blade['ctrl_pts']['update_r_in'] = True
        else:
            # Control point grid is passed from the outside as r_in, no need to update it when chord changes
            blade['ctrl_pts']['update_r_in'] = False

        blade['ctrl_pts']['r_in']         = r_in

        # Fit control points to planform variables
        blade['ctrl_pts']['theta_in']     = remap2grid(blade['pf']['s'], blade['pf']['theta'], r_in)
        blade['ctrl_pts']['chord_in']     = remap2grid(blade['pf']['s'], blade['pf']['chord'], r_in)
        blade['ctrl_pts']['precurve_in']  = remap2grid(blade['pf']['s'], blade['pf']['precurve'], r_in)
        blade['ctrl_pts']['presweep_in']  = remap2grid(blade['pf']['s'], blade['pf']['presweep'], r_in)
        blade['ctrl_pts']['thickness_in'] = remap2grid(blade['pf']['s'], blade['pf']['rthick'], r_in)

        # Fit control points to composite thickness variables variables
        #   Note: entering 0 thickness for areas where composite section does not extend to, however the precomp region selection vars
        #   sector_idx_strain_spar, sector_idx_strain_te) will still be None over these ranges
        idx_spar  = [i for i, sec in enumerate(blade['st']['layers']) if sec['name'].lower()==self.spar_var[0].lower()][0]
        idx_te    = [i for i, sec in enumerate(blade['st']['layers']) if sec['name'].lower()==self.te_var.lower()][0]
        grid_spar = blade['st']['layers'][idx_spar]['thickness']['grid']
        grid_te   = blade['st']['layers'][idx_te]['thickness']['grid']
        vals_spar = [0. if val==None else val for val in blade['st']['layers'][idx_spar]['thickness']['values']]
        vals_te   = [0. if val==None else val for val in blade['st']['layers'][idx_te]['thickness']['values']]
        blade['ctrl_pts']['sparT_in']     = remap2grid(grid_spar, vals_spar, r_in)
        blade['ctrl_pts']['teT_in']       = remap2grid(grid_te, vals_te, r_in)

        # Store additional rotorse variables
        blade['ctrl_pts']['r_cylinder']   = r_cylinder
        blade['ctrl_pts']['r_max_chord']  = r_max_chord
        # blade['ctrl_pts']['bladeLength']  = arc_length(blade['pf']['precurve'], blade['pf']['presweep'], blade['pf']['r'])[-1]
        blade['ctrl_pts']['bladeLength']  = blade['pf']['r'][-1]

        # plt.plot(r_in, blade['ctrl_pts']['thickness_in'], 'x')
        # plt.show()

        return blade

    def update_planform(self, blade):

        af_ref = blade['AFref']

        if blade['ctrl_pts']['update_r_in']:
            blade['ctrl_pts']['r_in'] = np.hstack([0., blade['ctrl_pts']['r_cylinder'], np.linspace(blade['ctrl_pts']['r_max_chord'][0], 1., self.NINPUT-2)])

        # if blade['ctrl_pts']['r_in'][3] != blade['ctrl_pts']['r_max_chord'] and not blade['ctrl_pts']['Fix_r_in']:
            # # blade['ctrl_pts']['r_in'] = np.r_[[0.], [blade['ctrl_pts']['r_cylinder']], np.linspace(blade['ctrl_pts']['r_max_chord'], 1., self.NINPUT-2)]
            # blade['ctrl_pts']['r_in'] = np.concatenate([[0.], np.linspace(blade['ctrl_pts']['r_cylinder'], blade['ctrl_pts']['r_max_chord'], num=3)[:-1], np.linspace(blade['ctrl_pts']['r_max_chord'], 1., self.NINPUT-3)])

        self.s                  = blade['pf']['s'] # TODO: assumes the start and end points of composite sections does not change
        blade['pf']['chord']    = remap2grid(blade['ctrl_pts']['r_in'], blade['ctrl_pts']['chord_in'], self.s)
        blade['pf']['theta']    = remap2grid(blade['ctrl_pts']['r_in'], blade['ctrl_pts']['theta_in'], self.s)
        blade['pf']['r']        = np.array(self.s)*blade['ctrl_pts']['bladeLength']
        blade['pf']['precurve'] = remap2grid(blade['ctrl_pts']['r_in'], blade['ctrl_pts']['precurve_in'], self.s)
        blade['pf']['presweep'] = remap2grid(blade['ctrl_pts']['r_in'], blade['ctrl_pts']['presweep_in'], self.s)

        thk_ref = [af_ref[af]['relative_thickness'] for af in blade['outer_shape_bem']['airfoil_position']['labels']]
        blade['pf']['rthick']   = remap2grid(blade['outer_shape_bem']['airfoil_position']['grid'], thk_ref, self.s)
        # Smooth oscillation caused by interpolation after min thickness is reached
        idx_min = [i for i, thk in enumerate(blade['pf']['rthick']) if thk == min(thk_ref)]
        if len(idx_min) > 0:
            blade['pf']['rthick']   = np.array([min(thk_ref) if i > idx_min[0] else thk for i, thk in enumerate(blade['pf']['rthick'])])

        # blade['ctrl_pts']['bladeLength']  = arc_length(blade['pf']['precurve'], blade['pf']['presweep'], blade['pf']['r'])[-1]

        for var in self.spar_var:
            idx_spar  = [i for i, sec in enumerate(blade['st']['layers']) if sec['name'].lower()==var.lower()][0]
            blade['st']['layers'][idx_spar]['thickness']['grid']   = self.s.tolist()
            blade['st']['layers'][idx_spar]['thickness']['values'] = remap2grid(blade['ctrl_pts']['r_in'], blade['ctrl_pts']['sparT_in'], self.s).tolist()

        idx_te    = [i for i, sec in enumerate(blade['st']['layers']) if sec['name'].lower()==self.te_var.lower()][0]
        blade['st']['layers'][idx_te]['thickness']['grid']   = self.s.tolist()
        blade['st']['layers'][idx_te]['thickness']['values'] = remap2grid(blade['ctrl_pts']['r_in'], blade['ctrl_pts']['teT_in'], self.s).tolist()

        # blade['pf']['rthick']   = remap2grid(blade['ctrl_pts']['r_in'], blade['ctrl_pts']['thickness_in'], self.s)
        # # update airfoil positions
        # # this only gets used in ontology file outputting
        # af_name_ref = list(blade['AFref'])
        # af_thk_ref  = [blade['AFref'][name]['relative_thickness'] for name in af_name_ref]
        # blade['pf']['af_pos']      = []
        # blade['pf']['af_pos_name'] = []
        # # find iterpolated spanwise position for anywhere a reference airfoil occures, i.e. the spanwise relative thickness crosses a reference airfoil relative thickness
        # for i in range(0,len(self.s)):
        #     for j in range(len(af_name_ref)):
        #         if af_thk_ref[j] == blade['pf']['rthick'][i]:
        #             blade['pf']['af_pos'].append(float(self.s[i]))
        #             blade['pf']['af_pos_name'].append(af_name_ref[j])
        #         elif i > 0:
        #             if (blade['pf']['rthick'][i-1] <= af_thk_ref[j] <= blade['pf']['rthick'][i]) or (blade['pf']['rthick'][i-1] >= af_thk_ref[j] >= blade['pf']['rthick'][i]):
        #                 i_min = max(i-1, 0)
        #                 i_max = max(i+1, np.argmin(blade['pf']['rthick']))
        #                 r_j   = remap2grid(blade['pf']['rthick'][i_min:i_max], self.s[i_min:i_max], af_thk_ref[j])
        #                 blade['pf']['af_pos'].append(float(r_j))
        #                 blade['pf']['af_pos_name'].append(af_name_ref[j])
        # # remove interior duplicates where an airfoil is listed more than 2 times in a row
        # x = blade['pf']['af_pos']
        # y = blade['pf']['af_pos_name']
        # blade['pf']['af_pos']      = [x[0]] + [x[i] for i in range(1,len(y)-1) if not(y[i] == y[i-1] and y[i] == y[i+1])] + [x[-1]]
        # blade['pf']['af_pos_name'] = [y[0]] + [y[i] for i in range(1,len(y)-1) if not(y[i] == y[i-1] and y[i] == y[i+1])] + [y[-1]]
        sys.stdout.flush()
        return blade


    def convert_precomp(self, blade, materials_in=[]):

        ##############################
        def region_stacking(i, idx, start_nd_arc, end_nd_arc, blade, material_dict, materials, region_loc):
            # Recieve start and end of composite sections chordwise, find which composites layers are in each
            # chordwise regions, generate the precomp composite class instance

            # error handling to makes sure there were no numeric errors causing values very close too, but not exactly, 0 or 1
            start_nd_arc = [0. if start_nd_arci!=0. and np.isclose(start_nd_arci,0.) else start_nd_arci for start_nd_arci in start_nd_arc]
            end_nd_arc = [0. if end_nd_arci!=0. and np.isclose(end_nd_arci,0.) else end_nd_arci for end_nd_arci in end_nd_arc]
            start_nd_arc = [1. if start_nd_arci!=1. and np.isclose(start_nd_arci,1.) else start_nd_arci for start_nd_arci in start_nd_arc]
            end_nd_arc = [1. if end_nd_arci!=1. and np.isclose(end_nd_arci,1.) else end_nd_arci for end_nd_arci in end_nd_arc]

            # region end points
            dp = sorted(list(set(start_nd_arc+end_nd_arc)))

            #initialize
            n_plies = []
            thk = []
            theta = []
            mat_idx = []

            # loop through division points, find what layers make up the stack between those bounds
            for i_reg, (dp0, dp1) in enumerate(zip(dp[0:-1], dp[1:])):
                n_pliesi = []
                thki     = []
                thetai   = []
                mati     = []
                for i_sec, start_nd_arci, end_nd_arci in zip(idx, start_nd_arc, end_nd_arc):
                    name = blade['st']['layers'][i_sec]['name']
                    if start_nd_arci <= dp0 and end_nd_arci >= dp1:

                        if name in region_loc.keys():
                            if region_loc[name][i] == None:
                                region_loc[name][i] = [i_reg]
                            else:
                                region_loc[name][i].append(i_reg)

                        n_pliesi.append(1.)
                        thki.append(blade['st']['layers'][i_sec]['thickness']['values'][i])
                        if blade['st']['layers'][i_sec]['fiber_orientation']['values'][i] == None:
                            thetai.append(0.)
                        else:
                            thetai.append(blade['st']['layers'][i_sec]['fiber_orientation']['values'][i])
                        mati.append(material_dict[blade['st']['layers'][i_sec]['material']])

                n_plies.append(np.array(n_pliesi))
                thk.append(np.array(thki))
                theta.append(np.array(thetai))
                mat_idx.append(np.array(mati))

            # print('----------------------')
            # print('dp', dp)
            # print('n_plies', n_plies)
            # print('thk', thk)
            # print('theta', theta)
            # print('mat_idx', mat_idx)
            # print('materials', materials)

            sec = CompositeSection(dp, n_plies, thk, theta, mat_idx, materials)
            return sec, region_loc
            ##############################

        def web_stacking(i, web_idx, web_start_nd_arc, web_end_nd_arc, blade, material_dict, materials, flatback, upperCSi):
            dp = []
            n_plies = []
            thk = []
            theta = []
            mat_idx = []

            if len(web_idx)>0:
                dp = np.mean((np.abs(web_start_nd_arc), np.abs(web_start_nd_arc)), axis=0).tolist()

                dp_all = [[-1.*start_nd_arci, -1.*end_nd_arci] for start_nd_arci, end_nd_arci in zip(web_start_nd_arc, web_end_nd_arc)]
                web_dp, web_ids = np.unique(dp_all, axis=0, return_inverse=True)
                for webi in np.unique(web_ids):
                    # store variable values (thickness, orientation, material) for layers that make up each web, based on the mapping array web_ids
                    n_pliesi = [1. for i_reg, web_idi in zip(web_idx, web_ids) if web_idi==webi]
                    thki     = [blade['st']['layers'][i_reg]['thickness']['values'][i] for i_reg, web_idi in zip(web_idx, web_ids) if web_idi==webi]
                    thetai   = [blade['st']['layers'][i_reg]['fiber_orientation']['values'][i] for i_reg, web_idi in zip(web_idx, web_ids) if web_idi==webi]
                    thetai   = [0. if theta_ij==None else theta_ij for theta_ij in thetai]
                    mati     = [material_dict[blade['st']['layers'][i_reg]['material']] for i_reg, web_idi in zip(web_idx, web_ids) if web_idi==webi]

                    n_plies.append(np.array(n_pliesi))
                    thk.append(np.array(thki))
                    theta.append(np.array(thetai))
                    mat_idx.append(np.array(mati))

            if flatback:
                dp.append(1.)
                n_plies.append(upperCSi.n_plies[-1])
                thk.append(upperCSi.t[-1])
                theta.append(upperCSi.theta[-1])
                mat_idx.append(upperCSi.mat_idx[-1])

            dp_out = sorted(list(set(dp)))

            sec = CompositeSection(dp_out, n_plies, thk, theta, mat_idx, materials)
            return sec
            ##############################

        ## Initialization
        if 'precomp' not in blade.keys():
            blade['precomp'] = {}

        region_loc_vars = [self.te_var] + self.spar_var
        region_loc_ss = {} # track precomp regions for user selected composite layers
        region_loc_ps = {}
        for var in region_loc_vars:
            region_loc_ss[var] = [None]*self.NPTS
            region_loc_ps[var] = [None]*self.NPTS


        ## Materials
        if 'materials' not in blade['precomp']:
            material_dict = {}
            materials     = []
            for i, mati in enumerate(materials_in):
                if mati['orth'] == 1 or mati['orth'] == True:
                    try:
                        iter(mati['E'])
                    except:
                        warnings.warn('Ontology input warning: Material "%s" entered as Orthogonal, must supply E, G, and nu as a list representing the 3 principle axes.'%mati['name'])
                if 'G' not in mati.keys():

                    if mati['orth'] == 1 or mati['orth'] == True:
                        warning_shear_modulus_orthogonal = 'Ontology input warning: No shear modulus, G, provided for material "%s".'%mati['name']
                        warnings.warn(warning_shear_modulus_orthogonal)
                    else:
                        warning_shear_modulus_isotropic = 'Ontology input warning: No shear modulus, G, provided for material "%s".  Assuming 2G*(1 + nu) = E, which is only valid for isotropic materials.'%mati['name']
                        warnings.warn(warning_shear_modulus_isotropic)
                        mati['G'] = mati['E']/(2*(1+mati['nu']))

                material_id = i
                material_dict[mati['name']] = material_id
                if mati['orth'] == 1 or mati['orth'] == True:
                    materials.append(Orthotropic2DMaterial(mati['E'][0], mati['E'][1], mati['G'][0], mati['nu'][0], mati['rho'], mati['name']))
                else:
                    materials.append(Orthotropic2DMaterial(mati['E'], mati['E'], mati['G'], mati['nu'], mati['rho'], mati['name']))
            blade['precomp']['materials']     = materials
            blade['precomp']['material_dict'] = material_dict


        upperCS = [None]*self.NPTS
        lowerCS = [None]*self.NPTS
        websCS  = [None]*self.NPTS
        profile = [None]*self.NPTS

        ## Spanwise
        for i in range(self.NPTS):
            # time0 = time.time()

            ## Profiles
            # rotate
            profile_i = np.flip(copy.copy(blade['profile'][:,:,i]), axis=0)
            profile_i_rot = np.column_stack(rotate(blade['pf']['p_le'][i], 0., profile_i[:,0], profile_i[:,1], -1.*np.radians(blade['pf']['theta'][i])))
            # normalize
            profile_i_rot[:,0] -= min(profile_i_rot[:,0])
            profile_i_rot = profile_i_rot/ max(profile_i_rot[:,0])

            profile_i_rot_precomp = copy.copy(profile_i_rot)
            idx_s = 0
            idx_le_precomp = np.argmax(profile_i_rot_precomp[:,0])
            if idx_le_precomp != 0:

                if profile_i_rot_precomp[0,0] == profile_i_rot_precomp[-1,0]:
                     idx_s = 1
                profile_i_rot_precomp = np.row_stack((profile_i_rot_precomp[idx_le_precomp:], profile_i_rot_precomp[idx_s:idx_le_precomp,:]))
            profile_i_rot_precomp[:,1] -= profile_i_rot_precomp[np.argmin(profile_i_rot_precomp[:,0]),1]

            # # renormalize
            profile_i_rot_precomp[:,0] -= min(profile_i_rot_precomp[:,0])
            profile_i_rot_precomp = profile_i_rot_precomp/ max(profile_i_rot_precomp[:,0])

            if profile_i_rot_precomp[-1,0] != 1.:
                profile_i_rot_precomp = np.row_stack((profile_i_rot_precomp, profile_i_rot_precomp[0,:]))

            # 'web' at trailing edge needed for flatback airfoils
            if profile_i_rot_precomp[0,1] != profile_i_rot_precomp[-1,1] and profile_i_rot_precomp[0,0] == profile_i_rot_precomp[-1,0]:
                flatback = True
            else:
                flatback = False

            profile[i] = Profile.initWithTEtoTEdata(profile_i_rot_precomp[:,0], profile_i_rot_precomp[:,1])

            # import matplotlib.pyplot as plt
            # plt.plot(profile_i_rot_precomp[:,0], profile_i_rot_precomp[:,1])
            # plt.axis('equal')
            # plt.title(i)
            # plt.show()

            idx_le = np.argmin(profile_i_rot[:,0])

            profile_i_arc = arc_length(profile_i_rot[:,0], profile_i_rot[:,1])
            arc_L = profile_i_arc[-1]
            profile_i_arc /= arc_L

            loc_LE = profile_i_arc[idx_le]
            len_PS = 1.-loc_LE

            ## Composites
            ss_idx           = []
            ss_start_nd_arc  = []
            ss_end_nd_arc    = []
            ps_idx           = []
            ps_start_nd_arc  = []
            ps_end_nd_arc    = []
            web_start_nd_arc = []
            web_end_nd_arc   = []
            web_idx          = []

            # Determine spanwise composite layer elements that are non-zero at this spanwise location,
            # determine their chord-wise start and end location on the pressure and suctions side

            spline_arc2xnd = PchipInterpolator(profile_i_arc, profile_i_rot[:,0])

            time1 = time.time()
            for idx_sec, sec in enumerate(blade['st']['layers']):

                if 'web' not in sec.keys():
                    if sec['start_nd_arc']['values'][i] != None and sec['thickness']['values'][i] != None:
                        if sec['start_nd_arc']['values'][i] < loc_LE or sec['end_nd_arc']['values'][i] < loc_LE:
                            ss_idx.append(idx_sec)
                            if sec['start_nd_arc']['values'][i] < loc_LE:
                                # ss_start_nd_arc.append(sec['start_nd_arc']['values'][i])
                                ss_end_nd_arc_temp = float(spline_arc2xnd(sec['start_nd_arc']['values'][i]))
                                if ss_end_nd_arc_temp == profile_i_rot[0,0] and profile_i_rot[0,0] != 1.:
                                    ss_end_nd_arc_temp = 1.
                                ss_end_nd_arc.append(ss_end_nd_arc_temp)
                            else:
                                ss_end_nd_arc.append(1.)
                            # ss_end_nd_arc.append(min(sec['end_nd_arc']['values'][i], loc_LE)/loc_LE)
                            if sec['end_nd_arc']['values'][i] < loc_LE:
                                ss_start_nd_arc.append(float(spline_arc2xnd(sec['end_nd_arc']['values'][i])))
                            else:
                                ss_start_nd_arc.append(0.)

                        if sec['start_nd_arc']['values'][i] > loc_LE or sec['end_nd_arc']['values'][i] > loc_LE:
                            ps_idx.append(idx_sec)
                            # ps_start_nd_arc.append((max(sec['start_nd_arc']['values'][i], loc_LE)-loc_LE)/len_PS)
                            # ps_end_nd_arc.append((min(sec['end_nd_arc']['values'][i], 1.)-loc_LE)/len_PS)

                            if sec['start_nd_arc']['values'][i] > loc_LE and sec['end_nd_arc']['values'][i] < loc_LE:
                                # ps_start_nd_arc.append(float(remap2grid(profile_i_arc, profile_i_rot[:,0], sec['start_nd_arc']['values'][i])))
                                ps_end_nd_arc.append(1.)
                            else:
                                ps_end_nd_arc_temp = float(spline_arc2xnd(sec['end_nd_arc']['values'][i]))
                                if np.isclose(ps_end_nd_arc_temp, profile_i_rot[-1,0]) and profile_i_rot[-1,0] != 1.:
                                    ps_end_nd_arc_temp = 1.
                                ps_end_nd_arc.append(ps_end_nd_arc_temp)
                            if sec['start_nd_arc']['values'][i] < loc_LE:
                                ps_start_nd_arc.append(0.)
                            else:
                                ps_start_nd_arc.append(float(spline_arc2xnd(sec['start_nd_arc']['values'][i])))


                else:
                    target_name  = blade['st']['layers'][idx_sec]['web']
                    target_idx   = [k for k, webi in enumerate(blade['st']['webs']) if webi['name']==target_name][0]

                    if blade['st']['webs'][target_idx]['start_nd_arc']['values'][i] != None and blade['st']['layers'][idx_sec]['thickness']['values'][i] != None:
                        web_idx.append(idx_sec)

                        start_nd_arc = float(spline_arc2xnd(blade['st']['webs'][target_idx]['start_nd_arc']['values'][i]))
                        end_nd_arc   = float(spline_arc2xnd(blade['st']['webs'][target_idx]['end_nd_arc']['values'][i]))

                        web_start_nd_arc.append(start_nd_arc)
                        web_end_nd_arc.append(end_nd_arc)

            time1 = time.time() - time1
            # print(time1)

            # generate the Precomp composite stacks for chordwise regions
            upperCS[i], region_loc_ss = region_stacking(i, ss_idx, ss_start_nd_arc, ss_end_nd_arc, blade, blade['precomp']['material_dict'], blade['precomp']['materials'], region_loc_ss)
            lowerCS[i], region_loc_ps = region_stacking(i, ps_idx, ps_start_nd_arc, ps_end_nd_arc, blade, blade['precomp']['material_dict'], blade['precomp']['materials'], region_loc_ps)
            if len(web_idx)>0 or flatback:
                websCS[i] = web_stacking(i, web_idx, web_start_nd_arc, web_end_nd_arc, blade, blade['precomp']['material_dict'], blade['precomp']['materials'], flatback, upperCS[i])
            else:
                websCS[i] = CompositeSection([], [], [], [], [], [])


        blade['precomp']['upperCS']       = upperCS
        blade['precomp']['lowerCS']       = lowerCS
        blade['precomp']['websCS']        = websCS
        blade['precomp']['profile']       = profile

        # Assumptions:
        # - pressure and suction side regions are the same (i.e. spar cap is the Nth region on both side)
        # - if the composite layer is divided into multiple regions (i.e. if the spar cap is split into 3 regions due to the web locations),
        #   the middle region is selected with int(n_reg/2), note for an even number of regions, this rounds up
        blade['precomp']['sector_idx_strain_spar_ss'] = [None if regs==None else regs[int(len(regs)/2)] for regs in region_loc_ss[self.spar_var[0]]]
        blade['precomp']['sector_idx_strain_spar_ps'] = [None if regs==None else regs[int(len(regs)/2)] for regs in region_loc_ps[self.spar_var[1]]]
        blade['precomp']['sector_idx_strain_te_ss']   = [None if regs==None else regs[int(len(regs)/2)] for regs in region_loc_ss[self.te_var]]
        blade['precomp']['sector_idx_strain_te_ps']   = [None if regs==None else regs[int(len(regs)/2)] for regs in region_loc_ps[self.te_var]]
        blade['precomp']['spar_var'] = self.spar_var
        blade['precomp']['te_var']   = self.te_var

        return blade

    def plot_design(self, blade, path, show_plots = True):

        import matplotlib.pyplot as plt

        # Chord
        fc, axc  = plt.subplots(1,1,figsize=(5.3, 4))
        axc.plot(blade['pf']['s'], blade['pf']['chord'])
        axc.set(xlabel='r/R' , ylabel='Chord (m)')
        fig_name = 'init_chord.png'
        fc.savefig(path + fig_name)

        # Theta
        ft, axt  = plt.subplots(1,1,figsize=(5.3, 4))
        axt.plot(blade['pf']['s'], blade['pf']['theta'])
        axt.set(xlabel='r/R' , ylabel='Twist (deg)')
        fig_name = 'init_theta.png'
        ft.savefig(path + fig_name)

        # Pitch axis
        fp, axp  = plt.subplots(1,1,figsize=(5.3, 4))
        axp.plot(blade['pf']['s'], blade['pf']['p_le']*100.)
        axp.set(xlabel='r/R' , ylabel='Pitch Axis (%)')
        fig_name = 'init_p_le.png'
        fp.savefig(path + fig_name)


        # Planform
        le = blade['pf']['p_le']*blade['pf']['chord']
        te = (1. - blade['pf']['p_le'])*blade['pf']['chord']

        fpl, axpl  = plt.subplots(1,1,figsize=(5.3, 4))
        axpl.plot(blade['pf']['s'], -le)
        axpl.plot(blade['pf']['s'], te)
        axpl.set(xlabel='r/R' , ylabel='Planform (m)')
        axpl.legend()
        fig_name = 'init_planform.png'
        fpl.savefig(path + fig_name)



        # Relative thickness
        frt, axrt  = plt.subplots(1,1,figsize=(5.3, 4))
        axrt.plot(blade['pf']['s'], blade['pf']['rthick']*100.)
        axrt.set(xlabel='r/R' , ylabel='Relative Thickness (%)')
        fig_name = 'init_rthick.png'
        frt.savefig(path + fig_name)

        # Absolute thickness
        fat, axat  = plt.subplots(1,1,figsize=(5.3, 4))
        axat.plot(blade['pf']['s'], blade['pf']['rthick']*blade['pf']['chord'])
        axat.set(xlabel='r/R' , ylabel='Absolute Thickness (m)')
        fig_name = 'init_absthick.png'
        fat.savefig(path + fig_name)

        # Prebend
        fpb, axpb  = plt.subplots(1,1,figsize=(5.3, 4))
        axpb.plot(blade['pf']['s'], blade['pf']['precurve'])
        axpb.set(xlabel='r/R' , ylabel='Prebend (m)')
        fig_name = 'init_prebend.png'
        fpb.savefig(path + fig_name)

        # Sweep
        fsw, axsw  = plt.subplots(1,1,figsize=(5.3, 4))
        axsw.plot(blade['pf']['s'], blade['pf']['presweep'])
        axsw.set(xlabel='r/R' , ylabel='Presweep (m)')
        fig_name = 'init_presweep.png'
        plt.subplots_adjust(left = 0.14)
        fsw.savefig(path + fig_name)

        idx_spar  = [i for i, sec in enumerate(blade['st']['layers']) if sec['name'].lower()==self.spar_var[0].lower()][0]
        idx_te    = [i for i, sec in enumerate(blade['st']['layers']) if sec['name'].lower()==self.te_var.lower()][0]
        idx_skin  = [i for i, sec in enumerate(blade['st']['layers']) if sec['name'].lower()=='shell_skin'][0]

        # Spar caps thickness
        fsc, axsc  = plt.subplots(1,1,figsize=(5.3, 4))
        axsc.plot(blade['st']['layers'][idx_spar]['thickness']['grid'], blade['st']['layers'][idx_spar]['thickness']['values'])
        axsc.set(xlabel='r/R' , ylabel='Spar Caps Thickness (m)')
        fig_name = 'init_sc.png'
        plt.subplots_adjust(left = 0.14)
        fsc.savefig(path + fig_name)

        # TE reinf thickness
        fte, axte  = plt.subplots(1,1,figsize=(5.3, 4))
        axte.plot(blade['st']['layers'][idx_te]['thickness']['grid'], blade['st']['layers'][idx_te]['thickness']['values'])
        axte.set(xlabel='r/R' , ylabel='TE Reinf. Thickness (m)')
        fig_name = 'init_te.png'
        plt.subplots_adjust(left = 0.14)
        fte.savefig(path + fig_name)

        # Skin
        fsk, axsk  = plt.subplots(1,1,figsize=(5.3, 4))
        axsk.plot(blade['st']['layers'][idx_skin]['thickness']['grid'], blade['st']['layers'][idx_skin]['thickness']['values'])
        axsk.set(xlabel='r/R' , ylabel='Shell Skin Thickness (m)')
        fig_name = 'init_skin.png'
        fsk.savefig(path + fig_name)


        if show_plots:
            plt.show()


        return None


    def smooth_outer_shape(self, blade, path, show_plots = True):

        s               = blade['pf']['s']

        # Absolute Thickness
        abs_thick_init  = blade['pf']['rthick']*blade['pf']['chord']
        s_interp_at     = np.array([0.0, 0.15, 0.4, 0.6, 0.8, 1.0 ])
        f_interp1       = interp1d(s,abs_thick_init)
        abs_thick_int1  = f_interp1(s_interp_at)
        f_interp2       = PchipInterpolator(s_interp_at,abs_thick_int1)
        abs_thick_int2  = f_interp2(s)

        import matplotlib.pyplot as plt



        # Chord
        chord_init      = blade['pf']['chord']
        s_interp_c      = np.array([0.0, 0.05, 0.2, 0.4, 0.6, 0.8, 0.9, 1.0 ])
        f_interp1       = interp1d(s,chord_init)
        chord_int1      = f_interp1(s_interp_c)
        f_interp2       = PchipInterpolator(s_interp_c,chord_int1)
        chord_int2      = f_interp2(s)

        fc, axc  = plt.subplots(1,1,figsize=(5.3, 4))
        axc.plot(s, chord_init, c='k', label='Initial')
        axc.plot(s_interp_c, chord_int1, 'ko', label='Interp Points')
        axc.plot(s, chord_int2, c='b', label='PCHIP')
        axc.set(xlabel='r/R' , ylabel='Chord (m)')
        fig_name = 'interp_chord.png'
        axc.legend()
        fc.savefig(path + fig_name)



        # Twist
        theta_init      = blade['pf']['theta']
        s_interp_t      = np.array([0.0, 0.05, 0.2, 0.4, 0.6, 0.8, 1.0 ])
        f_interp1       = interp1d(s,theta_init)
        theta_int1      = f_interp1(s_interp_t)
        f_interp2       = PchipInterpolator(s_interp_t,theta_int1)
        theta_int2      = f_interp2(s)

        fc, axc  = plt.subplots(1,1,figsize=(5.3, 4))
        axc.plot(s, theta_init, c='k', label='Initial')
        axc.plot(s_interp_t, theta_int1, 'ko', label='Interp Points')
        axc.plot(s, theta_int2, c='b', label='PCHIP')
        axc.set(xlabel='r/R' , ylabel='Twist (deg)')
        fig_name = 'interp_twist.png'
        axc.legend()
        fc.savefig(path + fig_name)

	# Prebend
        pb_init         = blade['pf']['precurve']
        s_interp_pb     = np.array([0.0, 0.05, 0.3,  1.0 ])
        f_interp1       = interp1d(s,pb_init)
        pb_int1      = f_interp1(s_interp_pb)
        f_interp2       = PchipInterpolator(s_interp_pb,pb_int1)
        pb_int2      = f_interp2(s)

        fpb, axpb  = plt.subplots(1,1,figsize=(5.3, 4))
        axpb.plot(s, pb_init, c='k', label='Initial')
        axpb.plot(s_interp_pb, pb_int1, 'ko', label='Interp Points')
        axpb.plot(s, pb_int2, c='b', label='PCHIP')
        axpb.set(xlabel='r/R' , ylabel='Prebend (m)')
        fig_name = 'interp_pb.png'
        axpb.legend()
        fpb.savefig(path + fig_name)


        # Relative thickness
        r_thick_interp = abs_thick_int2 / chord_int2
        r_thick_airfoils = np.array([0.18, 0.211, 0.241, 0.301, 0.36 , 0.50, 1.00])
        f_interp1        = interp1d(r_thick_interp,s)
        s_interp_rt      = f_interp1(r_thick_airfoils)
        f_interp2        = PchipInterpolator(np.flip(s_interp_rt, axis=0),np.flip(r_thick_airfoils, axis=0))
        r_thick_int2     = f_interp2(s)


        frt, axrt  = plt.subplots(1,1,figsize=(5.3, 4))
        axrt.plot(blade['pf']['s'], blade['pf']['rthick']*100., c='k', label='Initial')
        axrt.plot(blade['pf']['s'], r_thick_interp * 100., c='b', label='Interp')
        axrt.plot(s_interp_rt, r_thick_airfoils * 100., 'og', label='Airfoils')
        axrt.plot(blade['pf']['s'], r_thick_int2 * 100., c='g', label='Reconstructed')
        axrt.set(xlabel='r/R' , ylabel='Relative Thickness (%)')
        fig_name = 'interp_rthick.png'
        axrt.legend()
        frt.savefig(path + fig_name)


        fat, axat  = plt.subplots(1,1,figsize=(5.3, 4))
        axat.plot(s, abs_thick_init, c='k', label='Initial')
        axat.plot(s_interp_at, abs_thick_int1, 'ko', label='Interp Points')
        axat.plot(s, abs_thick_int2, c='b', label='PCHIP')
        axat.plot(s, r_thick_int2 * chord_int2, c='g', label='Reconstructed')
        axat.set(xlabel='r/R' , ylabel='Absolute Thickness (m)')
        fig_name = 'interp_abs_thick.png'
        axat.legend()
        fat.savefig(path + fig_name)


        # Pitch axis location
        pc_max_rt = np.zeros_like(s)
        for i in range(np.shape(blade['profile'])[2]):
            x        = np.linspace(0.05,0.95,100)
            le       = np.argmin(blade['profile'][:,0,i])
            x_ss_raw = blade['profile'][le:,0,i]
            y_ss_raw = blade['profile'][le:,1,i]
            x_ps_raw = np.flip(blade['profile'][:le,0,i], axis=0)
            y_ps_raw = np.flip(blade['profile'][:le,1,i], axis=0)
            f_ss     = interp1d(x_ss_raw,y_ss_raw)
            y_ss     = f_ss(x)
            f_ps     = interp1d(x_ps_raw,y_ps_raw)
            y_ps     = f_ps(x)


            i_max_rt = np.argmax(y_ss-y_ps)
            pc_max_rt[i] = x[i_max_rt]

            # fpa, axpa  = plt.subplots(1,1,figsize=(5.3, 4))
            # axpa.plot(x_ss_raw, y_ss_raw, c='k', label='ss')
            # axpa.plot(x_ps_raw, y_ps_raw, c='b', label='ps')
            # axpa.plot(pc_max_rt[i], 0, 'ob', label='max rt')
            # plt.axis('equal')
            # plt.show()


        s_interp_pa     = np.array([0.0, 0.25, 0.4, 0.6, 0.8, 1.0])
        f_interp1       = interp1d(s,pc_max_rt)
        pa_int1         = f_interp1(s_interp_pa)
        f_interp2       = PchipInterpolator(s_interp_pa,pa_int1)
        pa_int2         = f_interp2(s)

        fpa, axpa  = plt.subplots(1,1,figsize=(5.3, 4))
        axpa.plot(blade['pf']['s'], blade['pf']['p_le'], c='k', label='PA')
        axpa.plot(blade['pf']['s'], pc_max_rt, c='b', label='max rt')
        axpa.plot(s_interp_pa, pa_int1, 'og', label='ctrl max rt')
        axpa.plot(blade['pf']['s'], pa_int2, c='g', label='interp max rt')
        axpa.set(xlabel='r/R' , ylabel='Pitch Axis (-)')
        axpa.legend()
        fig_name = 'pitch_axis.png'
        fpa.savefig(path + fig_name)



        # Planform
        le_init = blade['pf']['p_le']*blade['pf']['chord']
        te_init = (1. - blade['pf']['p_le'])*blade['pf']['chord']

        s_interp_le     = np.array([0.0, 0.5, 0.8, 0.9, 1.0])
        f_interp1       = interp1d(s,le_init)
        le_int1         = f_interp1(s_interp_le)
        f_interp2       = PchipInterpolator(s_interp_le,le_int1)
        le_int2         = f_interp2(s)

        fpl, axpl  = plt.subplots(1,1,figsize=(5.3, 4))
        axpl.plot(blade['pf']['s'], -le_init, c='k', label='LE init')
        axpl.plot(blade['pf']['s'], -le_int2, c='b', label='LE smooth old pa')
        axpl.plot(blade['pf']['s'], -pa_int2 * blade['pf']['chord'], c='g', label='LE smooth new pa')
        axpl.plot(blade['pf']['s'], te_init, c='k', label='TE init')
        axpl.plot(blade['pf']['s'], blade['pf']['chord'] - le_int2, c='b', label='TE smooth old pa')
        axpl.plot(blade['pf']['s'], (1. - pa_int2) * blade['pf']['chord'], c='g', label='TE smooth new pa')
        axpl.set(xlabel='r/R' , ylabel='Planform (m)')
        axpl.legend()
        fig_name = 'interp_planform.png'
        fpl.savefig(path + fig_name)




        if show_plots:
            plt.show()



        return None



if __name__ == "__main__":

    ## File managment
    # fname_input        = "turbine_inputs/nrel5mw_mod_update.yaml"
    fname_input        = "/mnt/c/Users/egaertne/WISDEM/nrel15mw/design/turbine_inputs/NREL15MW_opt_v05.yaml"
    fname_output       = "turbine_inputs/testing_twist.yaml"
    flag_write_out     = False
    flag_write_precomp = False
    dir_precomp_out    = "turbine_inputs/precomp"

    ## Load and Format Blade
    tt = time.time()
    refBlade = ReferenceBlade()
    refBlade.verbose  = True
    refBlade.spar_var = ['Spar_cap_ss', 'Spar_cap_ps']
    refBlade.te_var   = 'TE_reinforcement'
    refBlade.NINPUT   = 8
    refBlade.NPTS     = 40
    # refBlade.r_in     = np.linspace(0.,1.,refBlade.NINPUT)
    refBlade.validate = False
    refBlade.fname_schema = "turbine_inputs/IEAontology_schema.yaml"

    blade = refBlade.initialize(fname_input)
    # idx_spar  = [i for i, sec in enumerate(blade['st']['layers']) if sec['name'].lower()==refBlade.spar_var[0].lower()][0]

    # blade['ctrl_pts']['chord_in'][-1] *= 0.5
    # blade = refBlade.update(blade)

    ## save output yaml
    if flag_write_out:
        t3 = time.time()
        refBlade.write_ontology(fname_output, blade, refBlade.wt_ref)
        if refBlade.verbose:
            print('Complete: Write Output: \t%f s'%(time.time()-t3))

    ## save precomp out
    if flag_write_precomp:
        t4 = time.time()
        materials = blade['precomp']['materials']
        upper     = blade['precomp']['upperCS']
        lower     = blade['precomp']['lowerCS']
        webs      = blade['precomp']['websCS']
        profile   = blade['precomp']['profile']
        chord     = blade['pf']['chord']
        twist     = blade['pf']['theta']
        p_le      = blade['pf']['p_le']
        precomp_write = PreCompWriter(dir_precomp_out, materials, upper, lower, webs, profile, chord, twist, p_le)
        precomp_write.execute()
        if refBlade.verbose:
            print('Complete: Write PreComp: \t%f s'%(time.time()-t4))

    ## post procesing
    # path_out = '/mnt/c/Users/egaertne/WISDEM/nrel15mw/design/outputs/NREL15MW_opt_v05/post'
    # refBlade.smooth_outer_shape(blade, path_out)
    # refBlade.plot_design(blade, path_out)

    ## testing
    # s1      = copy.deepcopy(blade['pf']['s'])
    # rthick1 = copy.deepcopy(blade['pf']['rthick'])
    # # blade['outer_shape_bem']['airfoil_position']['grid'] = [0.0, 0.02, 0.09734520488936911, 0.3929596998828168, 0.7284713048618933, 0.8404746119336132, 0.9144578690139064, 1.0]
    # blade['outer_shape_bem']['airfoil_position']['grid'] = [0.0, 0.02, 0.097, 0.15, 0.7284713048618933, 0.8404746119336132, 0.9144578690139064, 0.98,1.0]
    # blade = refBlade.update(blade)
    # s2      = blade['pf']['s']
    # rthick2 = blade['pf']['rthick']
    # import matplotlib.pyplot as plt
    # plt.plot(s1, rthick1, label="init")
    # plt.plot(s2, rthick2, label="mod")
    # plt.show()
