import openmdao.api as om


class DummyComponent(om.ExplicitComponent):
    def setup(self):
        self.add_output('landbosse_foo', val=1.0)

    def compute(self, inputs, outputs, discrete_inputs=None, discrete_outputs=None):
        outputs['landbosse_foo'] = 200
        print('################################################')
        print(f"Dummy {outputs['landbosse_foo']}")
        print('################################################')